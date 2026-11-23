import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse

import aiohttp
from pymongo import ReturnDocument

from app import services
from app.config import PLAN_RANK, settings
from app.db import cols

log = logging.getLogger(__name__)

PAYSTACK_API_URL = "https://api.paystack.co/transaction/initialize"
PLAN_NAIRA_PRICES = {
    "pro": settings.pro_naira_price,
    "growth": settings.growth_naira_price,
}


class PaystackError(Exception):
    """Paystack could not initialize a checkout transaction."""


class PaystackWebhookError(ValueError):
    """A signed Paystack webhook did not match a pending transaction."""


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    if not signature.isascii():
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(signature, expected)


async def initialize_transaction(user_id: int, plan: str) -> str:
    if plan not in PLAN_NAIRA_PRICES:
        raise PaystackError("Unknown subscription plan.")
    if not settings.paystack_secret_key:
        raise PaystackError("Paystack is not configured.")

    reference = uuid.uuid4().hex
    amount = PLAN_NAIRA_PRICES[plan] * 100
    await cols.payments.insert_one(
        {
            "_id": reference,
            "provider": "paystack",
            "user_id": user_id,
            "plan": plan,
            "amount": amount,
            "currency": "NGN",
            "status": "initializing",
            "created_at": services.now(),
        }
    )
    payload = {
        "email": f"telegram-{user_id}@example.com",
        "amount": amount,
        "currency": "NGN",
        "reference": reference,
        "metadata": {"telegram_id": str(user_id), "plan": plan},
    }

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                PAYSTACK_API_URL,
                json=payload,
                headers={"Authorization": f"Bearer {settings.paystack_secret_key}"},
            ) as response:
                response.raise_for_status()
                result = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        await cols.payments.update_one(
            {"_id": reference, "status": "initializing"},
            {"$set": {"status": "initialization_failed"}},
        )
        raise PaystackError("Paystack transaction initialization failed.") from e

    data = result.get("data") if isinstance(result, dict) else None
    checkout_url = data.get("authorization_url") if isinstance(data, dict) else None
    returned_reference = data.get("reference") if isinstance(data, dict) else None
    parsed_url = urlparse(checkout_url) if isinstance(checkout_url, str) else None
    if (
        not isinstance(result, dict)
        or result.get("status") is not True
        or returned_reference != reference
        or parsed_url is None
        or parsed_url.scheme != "https"
        or parsed_url.hostname != "checkout.paystack.com"
    ):
        await cols.payments.update_one(
            {"_id": reference, "status": "initializing"},
            {"$set": {"status": "initialization_failed"}},
        )
        raise PaystackError("Paystack returned an invalid checkout response.")

    await cols.payments.update_one(
        {"_id": reference, "status": "initializing"},
        {"$set": {"status": "initialized", "authorization_url": checkout_url}},
    )
    return checkout_url


async def handle_webhook_event(
    event: dict,
) -> tuple[int, str, datetime, str | None] | None:
    if event.get("event") != "charge.success":
        return None

    data = event.get("data")
    if not isinstance(data, dict) or data.get("status") != "success":
        raise PaystackWebhookError("Invalid successful charge event.")

    reference = data.get("reference")
    if not isinstance(reference, str) or not reference:
        raise PaystackWebhookError("Missing transaction reference.")
    payment = await cols.payments.find_one({"_id": reference, "provider": "paystack"})
    if payment is None:
        raise PaystackWebhookError("Unknown transaction reference.")
    if payment.get("status") == "completed":
        return None
    if payment.get("status") not in ("initializing", "initialized", "processing"):
        raise PaystackWebhookError("Transaction is not payable.")

    metadata = data.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("plan") != payment.get("plan")
        or str(metadata.get("telegram_id")) != str(payment.get("user_id"))
        or type(data.get("amount")) is not int
        or data.get("amount") != payment.get("amount")
        or data.get("currency") != payment.get("currency")
        or payment.get("currency") != "NGN"
        or payment.get("plan") not in PLAN_RANK
    ):
        raise PaystackWebhookError("Charge details do not match the initialized transaction.")

    if payment["status"] != "processing":
        user = await cols.users.find_one({"_id": payment["user_id"]})
        if user is None:
            raise PaystackWebhookError("Transaction user no longer exists.")
        now = services.now()
        current_expiry = user.get("premium_until")
        expiry = max(current_expiry or now, now) + timedelta(days=30)
        payment = await cols.payments.find_one_and_update(
            {
                "_id": reference,
                "status": {"$in": ["initializing", "initialized"]},
            },
            {
                "$set": {
                    "status": "processing",
                    "entitlement_until": expiry,
                    "paid_at": now,
                    "paystack_transaction_id": data.get("id"),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if payment is None:
            payment = await cols.payments.find_one({"_id": reference})
            if payment is None:
                raise PaystackWebhookError("Transaction disappeared during processing.")
            if payment.get("status") == "completed":
                return None
            if payment.get("status") != "processing":
                raise PaystackWebhookError("Transaction could not be claimed.")

    expiry = payment.get("entitlement_until")
    if not isinstance(expiry, datetime):
        raise PaystackWebhookError("Transaction has no entitlement expiry.")
    user = await cols.users.find_one({"_id": payment["user_id"]})
    if user is None:
        raise PaystackWebhookError("Transaction user no longer exists.")
    current_plan = services.current_plan(user)
    plan = payment["plan"]
    if PLAN_RANK[current_plan] > PLAN_RANK[plan]:
        plan = current_plan
    old_charge = (
        user.get("subscription_charge_id")
        if user.get("payment_provider") == "stars"
        else None
    )
    expiry = max(user.get("premium_until") or expiry, expiry)
    await cols.users.update_one(
        {"_id": payment["user_id"]},
        {
            "$max": {"premium_until": expiry},
            "$set": {
                "plan": plan,
                "payment_provider": "paystack",
                "subscription_charge_id": None,
            },
        },
    )

    completed = await cols.payments.find_one_and_update(
        {"_id": reference, "status": "processing"},
        {"$set": {"status": "completed", "completed_at": services.now()}},
        return_document=ReturnDocument.BEFORE,
    )
    if completed is None:
        return None
    return payment["user_id"], plan, expiry, old_charge

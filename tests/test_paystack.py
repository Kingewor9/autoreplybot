import hashlib
import hmac
import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost/test")

from app.paystack import (
    PaystackWebhookError,
    handle_webhook_event,
    verify_webhook_signature,
)


class PaystackWebhookTests(unittest.IsolatedAsyncioTestCase):
    def test_signature_verification(self):
        body = b'{"event":"charge.success"}'
        secret = "test-secret"
        signature = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()

        self.assertTrue(verify_webhook_signature(body, signature, secret))
        self.assertFalse(verify_webhook_signature(body + b" ", signature, secret))
        self.assertFalse(verify_webhook_signature(body, "invalid", secret))
        self.assertFalse(verify_webhook_signature(body, "\u00e9", secret))

    async def test_charge_must_match_initialized_amount(self):
        payment = {
            "_id": "reference",
            "provider": "paystack",
            "user_id": 123,
            "plan": "pro",
            "amount": 750000,
            "currency": "NGN",
            "status": "initialized",
        }
        event = {
            "event": "charge.success",
            "data": {
                "reference": "reference",
                "status": "success",
                "amount": 75000,
                "currency": "NGN",
                "metadata": {"telegram_id": "123", "plan": "pro"},
            },
        }
        payments = SimpleNamespace(find_one=AsyncMock(return_value=payment))
        users = SimpleNamespace(find_one=AsyncMock())
        with patch("app.paystack.cols.payments", payments), patch(
            "app.paystack.cols.users", users
        ):
            with self.assertRaises(PaystackWebhookError):
                await handle_webhook_event(event)
            users.find_one.assert_not_awaited()

    async def test_completed_charge_replay_does_not_reactivate(self):
        payment = {"_id": "reference", "provider": "paystack", "status": "completed"}
        event = {
            "event": "charge.success",
            "data": {"reference": "reference", "status": "success"},
        }
        payments = SimpleNamespace(find_one=AsyncMock(return_value=payment))
        users = SimpleNamespace(update_one=AsyncMock())
        with patch("app.paystack.cols.payments", payments), patch(
            "app.paystack.cols.users", users
        ):
            self.assertIsNone(await handle_webhook_event(event))
            users.update_one.assert_not_awaited()

    async def test_processing_retry_grants_only_one_month(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        payment = {
            "_id": "reference",
            "provider": "paystack",
            "user_id": 123,
            "plan": "pro",
            "amount": 750000,
            "currency": "NGN",
            "status": "initialized",
        }
        user = {"_id": 123, "plan": "free", "premium_until": None}
        event = {
            "event": "charge.success",
            "data": {
                "reference": "reference",
                "status": "success",
                "amount": 750000,
                "currency": "NGN",
                "metadata": {"telegram_id": "123", "plan": "pro"},
            },
        }
        completion_attempts = 0

        async def find_payment(query):
            self.assertEqual(query["_id"], "reference")
            return dict(payment)

        async def update_payment(query, update, **kwargs):
            nonlocal completion_attempts
            self.assertEqual(query["_id"], "reference")
            self.assertIn("return_document", kwargs)
            fields = update["$set"]
            if fields["status"] == "processing":
                payment.update(fields)
                return dict(payment)
            completion_attempts += 1
            if completion_attempts == 1:
                raise RuntimeError("simulate interruption after entitlement update")
            previous = dict(payment)
            payment.update(fields)
            return previous

        async def find_user(query):
            self.assertEqual(query["_id"], 123)
            return dict(user)

        async def update_user(query, update):
            self.assertEqual(query["_id"], 123)
            for key, value in update["$max"].items():
                user[key] = max(user.get(key) or value, value)
            user.update(update["$set"])

        payments = SimpleNamespace(
            find_one=AsyncMock(side_effect=find_payment),
            find_one_and_update=AsyncMock(side_effect=update_payment),
        )
        users = SimpleNamespace(
            find_one=AsyncMock(side_effect=find_user),
            update_one=AsyncMock(side_effect=update_user),
        )
        with patch("app.paystack.cols.payments", payments), patch(
            "app.paystack.cols.users", users
        ), patch("app.paystack.services.now", return_value=now):
            with self.assertRaises(RuntimeError):
                await handle_webhook_event(event)

            result = await handle_webhook_event(event)

        expiry = now + timedelta(days=30)
        self.assertEqual(result, (123, "pro", expiry, None))
        self.assertEqual(user["premium_until"], expiry)
        self.assertEqual(users.update_one.await_count, 2)


if __name__ == "__main__":
    unittest.main()

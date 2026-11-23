import asyncio
import json
import logging
from contextlib import asynccontextmanager

from aiogram.exceptions import TelegramAPIError
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request

from app import db
from app.bot import COMMANDS, bot, dp
from app.config import settings
from app.followups import run_followup_worker
from app.paystack import (
    PaystackWebhookError,
    handle_webhook_event,
    verify_webhook_signature,
)
from app.scheduler import run_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await bot.set_my_commands(COMMANDS)
    if settings.webhook_base_url:
        await bot.set_webhook(
            url=f"{settings.webhook_base_url}/webhook",
            secret_token=settings.webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
        )
        log.info("Webhook set to %s/webhook", settings.webhook_base_url)
    else:
        log.warning("No WEBHOOK_BASE_URL / RENDER_EXTERNAL_URL — webhook not registered")
    tasks = [asyncio.create_task(run_scheduler(bot)), asyncio.create_task(run_followup_worker(bot))]
    yield
    for t in tasks:
        t.cancel()
    await bot.session.close()
    await db.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def health():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if x_telegram_bot_api_secret_token != settings.webhook_secret:
        raise HTTPException(status_code=403)
    update = Update.model_validate(await request.json(), context={"bot": bot})
    try:
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("Error while handling update %s", update.update_id)
    return {"ok": True}  # always 200 so Telegram doesn't retry-flood us


@app.post("/paystack/webhook")
async def paystack_webhook(request: Request):
    if not settings.paystack_secret_key:
        raise HTTPException(status_code=503, detail="Paystack is not configured")

    body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")
    if not verify_webhook_signature(body, signature, settings.paystack_secret_key):
        raise HTTPException(status_code=401, detail="Invalid Paystack signature")

    try:
        event = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from e
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid event payload")

    try:
        payment = await handle_webhook_event(event)
    except PaystackWebhookError as e:
        log.warning("Rejected Paystack webhook: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e

    if payment is not None:
        user_id, plan, expiry, old_charge = payment
        cancellation_failed = False
        if old_charge:
            try:
                await bot.edit_user_star_subscription(
                    user_id=user_id,
                    telegram_payment_charge_id=old_charge,
                    is_canceled=True,
                )
            except TelegramAPIError:
                cancellation_failed = True
                log.exception("Could not cancel old Stars subscription for %s", user_id)
        renewal_note = (
            " Please cancel your previous Telegram Stars subscription in "
            "<b>Settings → My Stars → Subscriptions</b> to avoid another charge."
            if cancellation_failed
            else ""
        )
        try:
            await bot.send_message(
                user_id,
                f"🎉 Paystack payment received! Your {plan.title()} plan is active until "
                f"<b>{expiry:%d %b %Y}</b>.{renewal_note}",
            )
        except TelegramAPIError:
            log.exception("Could not notify user %s about Paystack payment", user_id)
    return {"ok": True}

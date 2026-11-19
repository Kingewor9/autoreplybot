"""Background loop that tells users when their paid plan is about to end / has ended."""
import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app import services
from app.config import PLAN_NAMES, settings
from app.db import cols
from app.keyboards import upgrade_kb

log = logging.getLogger(__name__)


async def _send(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=upgrade_kb())
    except TelegramAPIError as e:
        log.warning("Could not notify %s: %s", user_id, e)


def _fallback_text() -> str:
    return (
        f"You'll be on the Free plan: {settings.free_rule_limit} active rules and "
        f"{settings.free_reply_limit} replies/month, and follow-ups pause. "
        "Nothing is deleted — your other rules and follow-ups are saved and come back "
        "when you subscribe again."
    )


async def check_subscriptions(bot: Bot) -> None:
    now = services.now()
    soon = now + timedelta(days=settings.expiry_warning_days)

    expiring = await cols.users.find(
        {
            "premium_until": {"$gt": now, "$lte": soon},
            "$expr": {"$ne": ["$notified_expiring_for", "$premium_until"]},
        }
    ).to_list()
    for u in expiring:
        name = PLAN_NAMES.get(u.get("plan") or "pro", "Pro")
        await _send(
            bot,
            u["_id"],
            f"⏳ <b>Your {name} plan ends on {u['premium_until']:%d %b %Y}.</b>\n\n"
            "It renews automatically with Telegram Stars unless you cancelled it. "
            "If it lapses: " + _fallback_text(),
        )
        await cols.users.update_one(
            {"_id": u["_id"]}, {"$set": {"notified_expiring_for": u["premium_until"]}}
        )

    expired = await cols.users.find(
        {
            "premium_until": {"$lte": now},
            "$expr": {"$ne": ["$notified_expired_for", "$premium_until"]},
        }
    ).to_list()
    for u in expired:
        name = PLAN_NAMES.get(u.get("plan") or "pro", "Pro")
        await _send(bot, u["_id"], f"⌛ <b>Your {name} plan has ended.</b>\n\n" + _fallback_text())
        await cols.users.update_one(
            {"_id": u["_id"]}, {"$set": {"notified_expired_for": u["premium_until"]}}
        )


async def run_scheduler(bot: Bot) -> None:
    while True:
        try:
            await check_subscriptions(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Subscription check failed")
        await asyncio.sleep(settings.scheduler_interval)

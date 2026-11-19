"""Background worker: sends due follow-up steps and owner reminders."""
import asyncio
import html
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from app import services
from app.config import settings
from app.db import cols
from app.keyboards import open_chat_kb
from app.sender import send_rule_reply

log = logging.getLogger(__name__)

LEASE = timedelta(minutes=5)  # a claimed job is retried if the worker dies mid-send


async def _end_steps(key: str) -> None:
    await cols.convs.update_one({"_id": key}, {"$set": {"next_due_at": None}})
    await cols.convs.delete_one({"_id": key, "notify_at": None})  # nothing left to do


async def _process_step(bot: Bot, conv: dict) -> None:
    key = conv["_id"]
    owner = await services.get_user(conv["owner_id"])
    rule = await cols.rules.find_one({"_id": conv["rule_id"]})
    conn = await cols.connections.find_one({"_id": conv["conn_id"]})
    steps = (rule or {}).get("followups") or []
    idx = conv["next_step"]

    ok = bool(
        owner
        and rule
        and conn
        and conn.get("is_enabled")
        and conn.get("can_reply")
        and services.is_growth(owner)
        and idx < len(steps)
        and services.now() < conv["window_ends_at"]
        and await services.is_rule_active(owner, rule["_id"])
    )
    if not ok:
        await _end_steps(key)
        return

    # Mark the send first so an echo of our own message isn't mistaken for a manual reply.
    await cols.convs.update_one({"_id": key}, {"$set": {"last_bot_send_at": services.now()}})
    try:
        await send_rule_reply(bot, conv["chat_id"], steps[idx], business_connection_id=conv["conn_id"])
    except TelegramAPIError as e:  # e.g. the 24h window closed
        log.warning("Follow-up failed (owner %s, chat %s): %s", conv["owner_id"], conv["chat_id"], e)
        await _end_steps(key)
        return

    nxt = idx + 1
    if nxt < len(steps):
        t = services.now()
        await cols.convs.update_one(
            {"_id": key},
            {"$set": {
                "next_step": nxt,
                "next_due_at": t + timedelta(minutes=steps[nxt]["delay_min"]),
                "last_bot_send_at": t,
            }},
        )
    else:
        await _end_steps(key)


async def _process_reminder(bot: Bot, conv: dict) -> None:
    key = conv["_id"]
    owner = await services.get_user(conv["owner_id"])
    rule = await cols.rules.find_one({"_id": conv["rule_id"]})
    if owner and rule and services.is_growth(owner):
        who = html.escape(conv.get("contact_name") or "A contact")
        if conv.get("contact_username"):
            who += f" (@{html.escape(conv['contact_username'])})"
        waited = services.fmt_minutes(
            max(1, int((services.now() - conv["created_at"]).total_seconds() // 60))
        )
        text = (
            "🔔 <b>No reply yet</b>\n\n"
            f"{who} got your auto-reply for <b>{html.escape(rule['keyword'])}</b> "
            f"{waited} ago and hasn't responded. Time for a manual follow-up?"
        )
        url = (
            f"https://t.me/{conv['contact_username']}"
            if conv.get("contact_username")
            else f"tg://user?id={conv['contact_id']}"
        )
        try:
            try:
                await bot.send_message(owner["_id"], text, reply_markup=open_chat_kb(url))
            except TelegramBadRequest:  # link button refused (e.g. privacy settings)
                await bot.send_message(owner["_id"], text)
        except TelegramAPIError as e:
            log.warning("Could not send reminder to %s: %s", owner["_id"], e)
    await cols.convs.delete_one({"_id": key, "next_due_at": None})


async def _drain(bot: Bot) -> None:
    for _ in range(200):  # cap per tick
        now = services.now()
        conv = await cols.convs.find_one_and_update(
            {"next_due_at": {"$lte": now}},
            {"$set": {"next_due_at": now + LEASE}},
        )
        if not conv:
            break
        await _process_step(bot, conv)

    for _ in range(200):
        now = services.now()
        conv = await cols.convs.find_one_and_update(
            {"notify_at": {"$lte": now}},
            {"$set": {"notify_at": None}},
        )
        if not conv:
            break
        await _process_reminder(bot, conv)


async def run_followup_worker(bot: Bot) -> None:
    while True:
        try:
            await _drain(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Follow-up worker tick failed")
        await asyncio.sleep(settings.worker_poll_seconds)

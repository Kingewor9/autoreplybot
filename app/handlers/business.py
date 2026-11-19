import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, Message
from pymongo import ReturnDocument

from app import services
from app.config import settings
from app.db import cols
from app.handlers.common import BUSINESS_PATH
from app.keyboards import main_menu, upgrade_kb
from app.sender import send_rule_reply

log = logging.getLogger(__name__)
router = Router()


def _can_reply(conn: BusinessConnection) -> bool:
    rights = getattr(conn, "rights", None)  # newer Bot API
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(conn, "can_reply", True))  # older Bot API


@router.business_connection()
async def on_business_connection(conn: BusinessConnection, bot: Bot):
    can_reply = _can_reply(conn)
    prev = await cols.connections.find_one_and_update(
        {"_id": conn.id},
        {
            "$set": {
                "user_id": conn.user.id,
                "user_chat_id": conn.user_chat_id,
                "is_enabled": bool(conn.is_enabled),
                "can_reply": can_reply,
                "updated_at": services.now(),
            },
            "$setOnInsert": {"created_at": services.now()},
        },
        upsert=True,
        return_document=ReturnDocument.BEFORE,
    )
    await services.ensure_user(conn.user)
    was_active = bool(prev and prev.get("is_enabled") and prev.get("can_reply"))
    now_active = bool(conn.is_enabled and can_reply)

    try:
        if now_active and not was_active:
            await bot.send_message(
                conn.user_chat_id,
                "✅ <b>Connected!</b>\n\nYour account is linked. "
                "Tap below to set up your first auto-reply.",
                reply_markup=main_menu(),
            )
        elif conn.is_enabled and not can_reply:
            await bot.send_message(
                conn.user_chat_id,
                "⚠️ I'm connected but I'm <b>not allowed to reply</b>. Open "
                f"<b>{BUSINESS_PATH}</b> and enable <b>Reply to messages</b> for me.",
            )
        elif was_active and not now_active:
            await bot.send_message(
                conn.user_chat_id,
                "🔌 <b>Disconnected.</b> I've stopped replying on your account. "
                f"You can re-add me any time in {BUSINESS_PATH}.",
            )
    except TelegramAPIError as e:
        log.warning("Could not notify %s about connection change: %s", conn.user.id, e)


async def _notify_owner(bot: Bot, owner_id: int, notice: str, count: int) -> None:
    limit = settings.free_reply_limit
    if notice == "limit_near":
        text = (
            f"⚠️ <b>Heads up:</b> you've used <b>{count}/{limit}</b> free auto-replies this "
            f"month ({limit - count} left). Upgrade to Pro for unlimited replies."
        )
    else:
        text = (
            f"🚫 <b>Free reply limit reached.</b> You've used all <b>{limit}</b> auto-replies "
            f"this month, so replies are paused until "
            f"<b>{services.next_reset():%d %b %Y}</b> — or upgrade to Pro for unlimited replies."
        )
    try:
        await bot.send_message(owner_id, text, reply_markup=upgrade_kb())
    except TelegramAPIError as e:
        log.warning("Could not notify %s about usage: %s", owner_id, e)


@router.business_message()
async def on_business_message(message: Message, bot: Bot):
    conn_id = message.business_connection_id
    sender = message.from_user
    if not conn_id or sender is None or sender.is_bot:
        return

    conn = await cols.connections.find_one({"_id": conn_id})
    if not conn or not conn.get("is_enabled") or not conn.get("can_reply"):
        return
    owner_id, chat_id = conn["user_id"], message.chat.id

    if sender.id == owner_id:
        # The owner is talking in this chat themselves: stop any pending follow-ups.
        # (Never auto-reply to the owner's own messages: loop protection.)
        await services.cancel_if_manual(conn_id, chat_id)
        return

    # The contact wrote: they responded, so any earlier follow-up sequence is over.
    await services.cancel_conversation(conn_id, chat_id)

    text = message.text or message.caption
    if not text:
        return

    owner = await services.get_user(owner_id)
    if not owner:
        return

    rules = await services.list_rules(owner_id)
    active = rules[: services.rule_limit(owner)]  # downgraded users: only the first N are live
    rule = services.find_match(active, text)
    if not rule:
        return

    rule_id = str(rule["_id"])
    if await services.on_cooldown(rule_id, chat_id):
        return

    allowed, notice, count = await services.consume_reply(owner)
    if not allowed:
        return

    try:
        await send_rule_reply(bot, chat_id, rule, business_connection_id=conn_id)
    except TelegramAPIError as e:
        log.warning("Auto-reply failed for owner %s: %s", owner_id, e)
        await services.refund_reply(owner_id)
        return

    await services.start_cooldown(rule_id, chat_id)
    await cols.rules.update_one({"_id": rule["_id"]}, {"$inc": {"hits": 1}})
    await services.start_conversation(owner, rule, conn_id, message)
    if notice:
        await _notify_owner(bot, owner_id, notice, count)

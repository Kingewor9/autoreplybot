import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, Filter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pymongo import ReturnDocument

from app import services
from app.config import settings
from app.db import cols
from app.ui import show

log = logging.getLogger(__name__)
router = Router()

AUDIENCES = {
    "all": "All users",
    "connected": "Connected users",
    "free": "Free tier users",
    "pro": "Pro users",
    "growth": "Growth users",
}
ADMIN_FLOW_STEPS = {"admin_broadcast_content", "admin_broadcast_cta"}


def _is_admin(user_id: int | None) -> bool:
    return bool(settings.admin_telegram_id and user_id == settings.admin_telegram_id)


class AdminBroadcastFlow(Filter):
    async def __call__(self, message: Message) -> bool:
        if not message.from_user or not _is_admin(message.from_user.id):
            return False
        user = await services.get_user(message.from_user.id)
        flow = user.get("flow") if user else None
        return bool(
            flow
            and flow.get("mode") == "admin_broadcast"
            and flow.get("step") in ADMIN_FLOW_STEPS
        )


def _panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Refresh analytics", callback_data="admin:refresh"),
                InlineKeyboardButton(text="📣 Broadcast", callback_data="admin:broadcast"),
            ],
            [InlineKeyboardButton(text="⬅️ Main menu", callback_data="menu")],
        ]
    )


def _audience_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"admin:aud:{key}")]
            for key, label in AUDIENCES.items()
        ]
        + [[InlineKeyboardButton(text="⬅️ Admin panel", callback_data="admin:refresh")]]
    )


def _cta_keyboard(buttons: list[dict] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button["text"], url=button["url"])]
            for button in buttons
        ]
    )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Send broadcast", callback_data="admin:send"),
                InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:cancel"),
            ]
        ]
    )


async def _active_connected_ids() -> list[int]:
    ids = await cols.connections.distinct("user_id", {"is_enabled": True})
    return [user_id for user_id in ids if isinstance(user_id, int)]


async def _analytics() -> dict[str, int]:
    now = services.now()
    connected_ids = await _active_connected_ids()
    return {
        "total": await cols.users.count_documents({}),
        "connected": len(connected_ids),
        "free": await cols.users.count_documents(
            {
                "$or": [
                    {"plan": "free"},
                    {"premium_until": {"$exists": False}},
                    {"premium_until": None},
                    {"premium_until": {"$lte": now}},
                ]
            }
        ),
        "pro": await cols.users.count_documents(
            {"plan": {"$in": ["pro", None]}, "premium_until": {"$gt": now}}
        ),
        "growth": await cols.users.count_documents(
            {"plan": "growth", "premium_until": {"$gt": now}}
        ),
    }


async def _audience_ids(audience: str) -> list[int]:
    if audience == "connected":
        connected_ids = await _active_connected_ids()
        if not connected_ids:
            return []
        query = {"_id": {"$in": connected_ids}}
    elif audience == "free":
        now = services.now()
        query = {
            "$or": [
                {"plan": "free"},
                {"premium_until": {"$exists": False}},
                {"premium_until": None},
                {"premium_until": {"$lte": now}},
            ]
        }
    elif audience in ("pro", "growth"):
        plan_query = {"$in": ["pro", None]} if audience == "pro" else audience
        query = {"plan": plan_query, "premium_until": {"$gt": services.now()}}
    elif audience == "all":
        query = {}
    else:
        return []
    users = await cols.users.find(query, {"_id": 1}).to_list()
    return [user["_id"] for user in users]


async def _dashboard_text() -> str:
    counts = await _analytics()
    return (
        "🛠 <b>Admin panel</b>\n\n"
        f"👥 Total users: <b>{counts['total']}</b>\n"
        f"🔗 Connected users: <b>{counts['connected']}</b>\n"
        f"🆓 Free tier users: <b>{counts['free']}</b>\n"
        f"⭐ Pro users: <b>{counts['pro']}</b>\n"
        f"🚀 Growth users: <b>{counts['growth']}</b>"
    )


async def _show_dashboard(target, user_id: int) -> None:
    await services.set_flow(user_id, None)
    text = await _dashboard_text()
    if isinstance(target, CallbackQuery):
        await show(target, text, _panel_keyboard())
    else:
        await target.answer(text, reply_markup=_panel_keyboard())


@router.message(Command("admin"), F.chat.type == "private")
async def cmd_admin(message: Message):
    if not message.from_user or not _is_admin(message.from_user.id):
        return
    await services.ensure_user(message.from_user)
    await _show_dashboard(message, message.from_user.id)


@router.callback_query(F.data == "admin:refresh")
async def cb_admin_refresh(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Not authorized.", show_alert=True)
        return
    await cb.answer()
    await _show_dashboard(cb, cb.from_user.id)


@router.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Not authorized.", show_alert=True)
        return
    await services.set_flow(cb.from_user.id, None)
    await cb.answer()
    await show(cb, "📣 <b>Choose a broadcast audience:</b>", _audience_keyboard())


@router.callback_query(F.data.startswith("admin:aud:"))
async def cb_admin_audience(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Not authorized.", show_alert=True)
        return
    audience = cb.data.rsplit(":", 1)[-1]
    if audience not in AUDIENCES:
        await cb.answer("Unknown audience.", show_alert=True)
        return
    await services.set_flow(
        cb.from_user.id,
        {"mode": "admin_broadcast", "step": "admin_broadcast_content", "audience": audience},
    )
    await cb.answer()
    await show(
        cb,
        f"📣 Audience: <b>{AUDIENCES[audience]}</b>\n\n"
        "Send the broadcast text or a photo with an optional caption.",
        None,
    )


@router.callback_query(F.data.in_({"admin:addcta", "admin:skipcta"}))
async def cb_admin_cta_choice(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Not authorized.", show_alert=True)
        return
    user = await services.get_user(cb.from_user.id)
    flow = user.get("flow") if user else None
    if not flow or flow.get("step") != "admin_broadcast_cta":
        await cb.answer("This broadcast step has expired.", show_alert=True)
        return
    await cb.answer()
    if cb.data == "admin:addcta":
        await show(
            cb,
            "🔘 Send CTA buttons, one per line:\n"
            "<code>Button label - https://example.com</code>\n\n"
            f"Up to {settings.max_buttons} buttons. Or tap Skip.",
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="⏭ Skip buttons", callback_data="admin:skipcta")]
                ]
            ),
        )
        return
    flow["draft"]["buttons"] = []
    await _show_broadcast_preview(cb.bot, cb.from_user.id, flow)


@router.message(F.chat.type == "private", AdminBroadcastFlow())
async def on_admin_broadcast_input(message: Message):
    user = await services.get_user(message.from_user.id)
    flow = user.get("flow") if user else None
    if not flow:
        return
    if flow["step"] == "admin_broadcast_content":
        if message.photo:
            caption = message.caption or ""
            if len(caption) > 1024:
                await message.answer("That caption is too long (max 1024 characters). Please resend.")
                return
            draft = {
                "photo_file_id": message.photo[-1].file_id,
                "text": message.html_caption if caption else "",
            }
        elif message.text:
            if len(message.text) > 4000:
                await message.answer("That message is too long (max 4000 characters). Please resend.")
                return
            draft = {"photo_file_id": None, "text": message.html_text}
        else:
            await message.answer("Please send text or a photo with an optional caption.")
            return
        flow["draft"] = draft
        flow["step"] = "admin_broadcast_cta"
        await services.set_flow(message.from_user.id, flow)
        await message.answer(
            "Add CTA buttons to this broadcast?",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🔘 Add buttons", callback_data="admin:addcta"),
                        InlineKeyboardButton(text="⏭ Skip", callback_data="admin:skipcta"),
                    ],
                    [InlineKeyboardButton(text="✖️ Cancel", callback_data="admin:cancel")],
                ]
            ),
        )
        return

    buttons = services.parse_buttons(message.text or "")
    if buttons is None:
        await message.answer(
            "I couldn't read those buttons. Use one per line:\n"
            "<code>Button label - https://example.com</code>"
        )
        return
    flow["draft"]["buttons"] = buttons
    await _show_broadcast_preview(message.bot, message.from_user.id, flow)


async def _show_broadcast_preview(bot: Bot, admin_id: int, flow: dict) -> None:
    audience = flow["audience"]
    recipients = await _audience_ids(audience)
    if not recipients:
        await services.set_flow(admin_id, None)
        await bot.send_message(admin_id, f"There are no users in the {AUDIENCES[audience]} audience.")
        return
    flow["step"] = "admin_broadcast_confirm"
    await services.set_flow(admin_id, flow)
    draft = flow["draft"]
    markup = _cta_keyboard(draft.get("buttons"))
    if draft.get("photo_file_id"):
        await bot.send_photo(
            admin_id,
            draft["photo_file_id"],
            caption=draft.get("text") or None,
            reply_markup=markup,
        )
    else:
        await bot.send_message(admin_id, draft["text"], reply_markup=markup)
    await bot.send_message(
        admin_id,
        f"Preview above 👆\n\n"
        f"Audience: <b>{AUDIENCES[audience]}</b> · <b>{len(recipients)}</b> users\n"
        "Send this broadcast?",
        reply_markup=_confirm_keyboard(),
    )


async def _send_to_user(
    bot: Bot, user_id: int, draft: dict, markup: InlineKeyboardMarkup | None
) -> bool:
    async def send():
        if draft.get("photo_file_id"):
            await bot.send_photo(
                user_id,
                draft["photo_file_id"],
                caption=draft.get("text") or None,
                reply_markup=markup,
            )
        else:
            await bot.send_message(user_id, draft["text"], reply_markup=markup)

    try:
        await send()
    except TelegramRetryAfter as error:
        await asyncio.sleep(error.retry_after)
        try:
            await send()
        except TelegramAPIError as retry_error:
            log.warning("Broadcast delivery failed for %s after retry: %s", user_id, retry_error)
            return False
    except TelegramAPIError as error:
        log.warning("Broadcast delivery failed for %s: %s", user_id, error)
        return False
    return True


@router.callback_query(F.data == "admin:send")
async def cb_admin_send(cb: CallbackQuery, bot: Bot):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Not authorized.", show_alert=True)
        return
    flow = await cols.users.find_one_and_update(
        {
            "_id": cb.from_user.id,
            "flow.mode": "admin_broadcast",
            "flow.step": "admin_broadcast_confirm",
        },
        {"$set": {"flow.step": "admin_broadcast_sending"}},
        return_document=ReturnDocument.BEFORE,
    )
    if not flow:
        await cb.answer("This broadcast has already been sent or expired.", show_alert=True)
        return
    broadcast = flow["flow"]
    await cb.answer("Broadcast started.")
    recipients = await _audience_ids(broadcast["audience"])
    if not recipients:
        await services.set_flow(cb.from_user.id, None)
        await show(cb, "No users remain in that audience.", _panel_keyboard())
        return

    try:
        await cb.message.edit_text(f"📣 Sending to {len(recipients)} users…")
    except TelegramAPIError:
        log.info("Could not update broadcast status message for admin %s", cb.from_user.id)

    sent = failed = 0
    draft = broadcast["draft"]
    markup = _cta_keyboard(draft.get("buttons"))
    for user_id in recipients:
        if await _send_to_user(bot, user_id, draft, markup):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.05)

    await services.set_flow(cb.from_user.id, None)
    await show(
        cb,
        "✅ <b>Broadcast complete</b>\n\n"
        f"Audience: {AUDIENCES[broadcast['audience']]}\n"
        f"Delivered: <b>{sent}</b>\n"
        f"Failed: <b>{failed}</b>",
        _panel_keyboard(),
    )


@router.callback_query(F.data == "admin:cancel")
async def cb_admin_cancel(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer("Not authorized.", show_alert=True)
        return
    await cb.answer("Broadcast cancelled.")
    await _show_dashboard(cb, cb.from_user.id)

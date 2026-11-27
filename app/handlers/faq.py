"""Smart FAQ setup and the owner's saved business information."""
import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app import keyboards as kb
from app import services
from app.config import settings
from app.db import cols
from app.ui import show

log = logging.getLogger(__name__)
router = Router()
MAX_FAQ_LENGTH = 3000


def render_faq(user: dict) -> tuple[str, InlineKeyboardMarkup]:
    faq_text = user.get("faq_text") or ""
    enabled = bool(user.get("faq_enabled") and faq_text)
    if faq_text:
        preview = html.escape(faq_text[:500])
        if len(faq_text) > 500:
            preview += "…"
        text = (
            "🧠 <b>Smart FAQ (AI)</b>\n\n"
            f"Status: {'🟢 ON' if enabled else '🔴 OFF'}\n"
            f"Saved business info ({len(faq_text)}/{MAX_FAQ_LENGTH} characters):\n"
            f"<blockquote>{preview}</blockquote>\n\n"
            "When enabled, keyword rules are checked first. AI only replies when your "
            "saved information answers the message; otherwise it stays silent."
        )
    else:
        text = (
            "🧠 <b>Smart FAQ (AI)</b>\n\n"
            "Save your business details, prices, payment information, or common FAQs in "
            "plain text. I’ll check keyword rules first, then use this information to "
            "answer covered questions and stay silent for unrelated messages."
        )
    if not settings.gemini_api_key:
        text += (
            "\n\n⚠️ AI replies are unavailable until the bot administrator configures "
            "<code>GEMINI_API_KEY</code>."
        )
    return text, kb.faq_menu(enabled, bool(faq_text))


async def _in_faq_flow(message: Message):
    if not message.from_user:
        return False
    user = await services.get_user(message.from_user.id)
    flow = (user or {}).get("flow")
    if flow and flow.get("mode") == "faq" and flow.get("step") == "faq_text":
        return {"user": user}
    return False


@router.callback_query(F.data == "faq")
async def cb_faq(cb: CallbackQuery):
    user = await services.ensure_user(cb.from_user)
    await services.set_flow(user["_id"], None)
    await cb.answer()
    text, markup = render_faq(user)
    if not user.get("faq_text"):
        text += (
            f"\n\nSend up to {MAX_FAQ_LENGTH} characters. Avoid including private "
            "customer data or secrets."
        )
    await show(cb, text, markup)


@router.callback_query(F.data == "faq:edit")
async def cb_faq_edit(cb: CallbackQuery):
    user = await services.ensure_user(cb.from_user)
    await services.set_flow(
        user["_id"], {"step": "faq_text", "mode": "faq"}
    )
    await cb.answer()
    await show(
        cb,
        f"✏️ Send your business details or FAQs as plain text "
        f"(up to {MAX_FAQ_LENGTH} characters).\n\n"
        "Example: <i>Ad promo costs ₦10,000 per post. Payment goes to OPay. "
        "Delivery takes 2 hours.</i>",
        kb.cancel_kb(),
    )


@router.callback_query(F.data == "faq:toggle")
async def cb_faq_toggle(cb: CallbackQuery):
    user = await services.ensure_user(cb.from_user)
    if not settings.gemini_api_key:
        await cb.answer("AI replies aren't configured yet.", show_alert=True)
        return
    faq_text = user.get("faq_text") or ""
    if not faq_text:
        await cb.answer("Save your business info first.", show_alert=True)
        return
    enabled = not bool(user.get("faq_enabled"))
    await cols.users.update_one(
        {"_id": user["_id"]}, {"$set": {"faq_enabled": enabled}}
    )
    await cb.answer("Smart FAQ turned on." if enabled else "Smart FAQ turned off.")
    user["faq_enabled"] = enabled
    text, markup = render_faq(user)
    await show(cb, text, markup)


@router.callback_query(F.data == "faq:clear")
async def cb_faq_clear(cb: CallbackQuery):
    await cb.answer()
    await show(
        cb,
        "Clear your saved Smart FAQ information? Smart FAQ will also be turned off.",
        kb.faq_clear_kb(),
    )


@router.callback_query(F.data == "faq:clear_yes")
async def cb_faq_clear_yes(cb: CallbackQuery):
    user = await services.ensure_user(cb.from_user)
    await cols.users.update_one(
        {"_id": user["_id"]},
        {"$unset": {"faq_text": "", "faq_enabled": ""}},
    )
    await services.set_flow(user["_id"], None)
    await cb.answer("Business information cleared.")
    user.pop("faq_text", None)
    user.pop("faq_enabled", None)
    text, markup = render_faq(user)
    await show(cb, text, markup)


@router.message(F.chat.type == "private", _in_faq_flow)
async def on_faq_text(message: Message, user: dict):
    uid = user["_id"]
    if message.text and message.text.startswith("/"):
        await message.answer("Finish this step or tap Cancel.", reply_markup=kb.cancel_kb())
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Please send the business information as text.",
            reply_markup=kb.cancel_kb(),
        )
        return
    if len(text) > MAX_FAQ_LENGTH:
        await message.answer(
            f"That is {len(text)} characters. Please keep it under "
            f"{MAX_FAQ_LENGTH} characters and resend.",
            reply_markup=kb.cancel_kb(),
        )
        return

    await cols.users.update_one({"_id": uid}, {"$set": {"faq_text": text}})
    await services.set_flow(uid, None)
    user["faq_text"] = text
    text, markup = render_faq(user)
    await message.answer("✅ Business information saved.\n\n" + text, reply_markup=markup)

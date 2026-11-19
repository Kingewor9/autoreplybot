from aiogram import F, Bot, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app import services
from app.db import cols
from app.keyboards import main_menu
from app.ui import show

router = Router()

BUSINESS_PATH = "Settings → Telegram Business → Chatbots"


def setup_text(username: str) -> str:
    return (
        "<b>How to connect me to your account</b>\n"
        f"1. Open Telegram <b>{BUSINESS_PATH}</b>\n"
        f"2. Add <code>@{username}</code>\n"
        "3. Allow <b>Reply to messages</b> and choose which chats I can answer\n\n"
        "The moment you add me, I'll message you here."
    )


async def menu_text(bot: Bot, user_id: int, first: bool = False) -> str:
    me = await bot.me()
    connected = await cols.connections.find_one({"user_id": user_id, "is_enabled": True})
    head = ""
    if first:
        head = (
            "👋 <b>Welcome!</b> I send automatic replies from your personal Telegram "
            "account when someone messages you a keyword (for example <i>ads</i>).\n\n"
            "⚠️ <b>Telegram Premium is required.</b> Business chatbots only work on "
            "Premium accounts.\n\n"
        )
    if connected:
        return head + "✅ <b>Your account is connected.</b>\n\nWhat would you like to do?"
    return head + setup_text(me.username) + "\n\nYou can already prepare your rules below 👇"


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot):
    user = await services.ensure_user(message.from_user)
    await services.set_flow(user["_id"], None)
    await message.answer(await menu_text(bot, user["_id"], first=True), reply_markup=main_menu())


@router.message(Command("menu"), F.chat.type == "private")
async def cmd_menu(message: Message, bot: Bot):
    user = await services.ensure_user(message.from_user)
    await services.set_flow(user["_id"], None)
    await message.answer(await menu_text(bot, user["_id"]), reply_markup=main_menu())


@router.message(Command("cancel"), F.chat.type == "private")
async def cmd_cancel(message: Message, bot: Bot):
    user = await services.ensure_user(message.from_user)
    await services.set_flow(user["_id"], None)
    await message.answer("Cancelled.\n\n" + await menu_text(bot, user["_id"]), reply_markup=main_menu())


@router.callback_query(F.data.in_({"menu", "cancel"}))
async def cb_menu(cb: CallbackQuery, bot: Bot):
    user = await services.ensure_user(cb.from_user)
    await services.set_flow(user["_id"], None)
    await cb.answer()
    await show(cb, await menu_text(bot, user["_id"]), main_menu())

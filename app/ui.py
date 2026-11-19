from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup


async def show(cb: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the menu message in place; fall back to a new message if that's not possible."""
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        await cb.message.answer(text, reply_markup=markup)
    except AttributeError:  # inaccessible (old) message
        await cb.bot.send_message(cb.from_user.id, text, reply_markup=markup)

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from app.config import settings
from app.handlers import business, common, followups_ui, payments, rules

bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Order matters: the catch-all conversation handler in `rules` must come last.
dp.include_router(common.router)
dp.include_router(payments.router)
dp.include_router(business.router)
dp.include_router(followups_ui.router)
dp.include_router(rules.router)

COMMANDS = [
    BotCommand(command="menu", description="Open the main menu"),
    BotCommand(command="upgrade", description="Subscription / Upgrade"),
    BotCommand(command="cancel", description="Cancel what I'm doing"),
    BotCommand(command="paysupport", description="Payment help"),
]

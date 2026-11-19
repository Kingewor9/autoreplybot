"""Local development: run the bot with long polling instead of a webhook."""
import asyncio
import logging

from app import db
from app.bot import COMMANDS, bot, dp
from app.followups import run_followup_worker
from app.scheduler import run_scheduler


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await db.init()
    await bot.delete_webhook()
    await bot.set_my_commands(COMMANDS)
    tasks = [asyncio.create_task(run_scheduler(bot)), asyncio.create_task(run_followup_worker(bot))]
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for t in tasks:
            t.cancel()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

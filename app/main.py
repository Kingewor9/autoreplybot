import asyncio
import logging
from contextlib import asynccontextmanager

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request

from app import db
from app.bot import COMMANDS, bot, dp
from app.config import settings
from app.followups import run_followup_worker
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

import hashlib
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional in production
    pass


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    bot_token: str
    mongodb_uri: str
    mongodb_db: str
    webhook_base_url: str
    webhook_secret: str
    stars_price: int
    free_rule_limit: int
    free_reply_limit: int
    paid_rule_limit: int
    cooldown_seconds: int
    expiry_warning_days: int
    scheduler_interval: int
    max_buttons: int
    support_contact: str
    growth_price: int
    growth_rule_limit: int
    followup_max_steps: int
    followup_min_minutes: int
    followup_max_total_minutes: int
    notify_max_minutes: int
    worker_poll_seconds: int
    echo_guard_seconds: int


def load_settings() -> Settings:
    token = _required("BOT_TOKEN")
    base_url = os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    return Settings(
        bot_token=token,
        mongodb_uri=_required("MONGODB_URI"),
        mongodb_db=os.getenv("MONGODB_DB", "autoreply_bot"),
        webhook_base_url=base_url.rstrip("/"),
        webhook_secret=os.getenv("WEBHOOK_SECRET")
        or hashlib.sha256(token.encode()).hexdigest()[:48],
        stars_price=_int("STARS_PRICE", 250),
        free_rule_limit=_int("FREE_RULE_LIMIT", 2),
        free_reply_limit=_int("FREE_REPLY_LIMIT", 50),
        paid_rule_limit=_int("PAID_RULE_LIMIT", 10),
        cooldown_seconds=_int("COOLDOWN_SECONDS", 600),
        expiry_warning_days=_int("EXPIRY_WARNING_DAYS", 3),
        scheduler_interval=_int("SCHEDULER_INTERVAL", 1800),
        max_buttons=_int("MAX_BUTTONS", 3),
        support_contact=os.getenv("SUPPORT_CONTACT", "the bot owner"),
        growth_price=_int("STARS_PRICE_GROWTH", 500),
        growth_rule_limit=_int("GROWTH_RULE_LIMIT", 20),
        followup_max_steps=_int("FOLLOWUP_MAX_STEPS", 3),
        followup_min_minutes=_int("FOLLOWUP_MIN_MINUTES", 5),
        # Telegram only allows replies within 24h of the contact's last message; keep 1h of margin
        followup_max_total_minutes=_int("FOLLOWUP_MAX_TOTAL_MINUTES", 23 * 60),
        notify_max_minutes=_int("NOTIFY_MAX_MINUTES", 7 * 24 * 60),
        worker_poll_seconds=_int("WORKER_POLL_SECONDS", 20),
        echo_guard_seconds=_int("ECHO_GUARD_SECONDS", 10),
    )


PLAN_NAMES = {"free": "Free", "pro": "Pro", "growth": "Growth"}
PLAN_RANK = {"free": 0, "pro": 1, "growth": 2}

settings = load_settings()

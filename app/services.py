"""Business logic: plans, limits, rule matching, usage counting, cooldowns."""
import math
import re
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.db import cols


# ---------- time helpers ----------
def now() -> datetime:
    return datetime.now(timezone.utc)


def month_key(dt: datetime | None = None) -> str:
    return (dt or now()).strftime("%Y-%m")


def next_reset() -> datetime:
    d = now()
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc)


def normalize(text: str) -> str:
    return " ".join(text.casefold().split())


# ---------- plans ----------
def current_plan(user: dict) -> str:
    """'free' | 'pro' | 'growth' (a lapsed subscription counts as free)."""
    until = user.get("premium_until")
    if until and until > now():
        return user.get("plan") or "pro"
    return "free"


def is_paid(user: dict) -> bool:
    return current_plan(user) != "free"


def is_growth(user: dict) -> bool:
    return current_plan(user) == "growth"


def rule_limit(user: dict) -> int:
    return {
        "free": settings.free_rule_limit,
        "pro": settings.paid_rule_limit,
        "growth": settings.growth_rule_limit,
    }[current_plan(user)]


def reply_limit(user: dict) -> int | None:
    """None = unlimited."""
    return None if is_paid(user) else settings.free_reply_limit


async def is_rule_active(user: dict, rule_id) -> bool:
    rules = await list_rules(user["_id"])
    return any(r["_id"] == rule_id for r in rules[: rule_limit(user)])


# ---------- users ----------
async def ensure_user(tg_user) -> dict:
    return await cols.users.find_one_and_update(
        {"_id": tg_user.id},
        {
            "$set": {"first_name": tg_user.first_name, "username": tg_user.username},
            "$setOnInsert": {"created_at": now(), "flow": None, "premium_until": None},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


async def get_user(user_id: int) -> dict | None:
    return await cols.users.find_one({"_id": user_id})


async def set_flow(user_id: int, flow: dict | None) -> None:
    await cols.users.update_one({"_id": user_id}, {"$set": {"flow": flow}})


# ---------- rules ----------
async def list_rules(owner_id: int) -> list[dict]:
    return await cols.rules.find({"owner_id": owner_id}).sort("created_at", 1).to_list()


async def get_rule(rule_id: str, owner_id: int) -> dict | None:
    try:
        oid = ObjectId(rule_id)
    except (InvalidId, TypeError):
        return None
    return await cols.rules.find_one({"_id": oid, "owner_id": owner_id})


async def rule_exists(owner_id: int, keyword_norm: str, match_type: str, exclude_id=None) -> bool:
    flt = {"owner_id": owner_id, "keyword_norm": keyword_norm, "match_type": match_type}
    if exclude_id is not None:
        flt["_id"] = {"$ne": exclude_id}
    return await cols.rules.find_one(flt) is not None


async def create_rule(user: dict, draft: dict) -> tuple[dict | None, str | None]:
    uid = user["_id"]
    if await cols.rules.count_documents({"owner_id": uid}) >= rule_limit(user):
        return None, "limit"
    if await rule_exists(uid, draft["keyword_norm"], draft["match_type"]):
        return None, "duplicate"
    doc = {
        "owner_id": uid,
        "keyword": draft["keyword"],
        "keyword_norm": draft["keyword_norm"],
        "match_type": draft["match_type"],
        "reply_html": draft.get("reply_html", ""),
        "reply_plain": draft.get("reply_plain", ""),
        "photo_file_id": draft.get("photo_file_id"),
        "buttons": draft.get("buttons", []),
        "hits": 0,
        "created_at": now(),
    }
    res = await cols.rules.insert_one(doc)
    doc["_id"] = res.inserted_id
    return doc, None


def find_match(rules: list[dict], text: str) -> dict | None:
    """One reply per message: exact matches win, then the longest 'contains' keyword."""
    t = normalize(text)
    if not t:
        return None
    for r in rules:
        if r["match_type"] == "exact" and r["keyword_norm"] == t:
            return r
    hits = [
        r
        for r in rules
        if r["match_type"] == "contains"
        and re.search(rf"(?<!\w){re.escape(r['keyword_norm'])}(?!\w)", t)
    ]
    if not hits:
        return None
    hits.sort(key=lambda r: (-len(r["keyword_norm"]), r["created_at"]))
    return hits[0]


# ---------- buttons ----------
_BTN = re.compile(r"^(.+?)\s*[|\-\u2013\u2014]\s*((?:https?://|t\.me/)\S+)$", re.I)


def parse_buttons(text: str) -> list[dict] | None:
    """'Label - https://link' per line. Returns None when the input is invalid."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _BTN.match(line)
        if not m:
            return None
        label, url = m.group(1).strip(), m.group(2)
        if not label or len(label) > 40:
            return None
        if url.lower().startswith("t.me/"):
            url = "https://" + url
        out.append({"text": label, "url": url})
    if not out or len(out) > settings.max_buttons:
        return None
    return out


# ---------- cooldown (spam protection) ----------
async def on_cooldown(rule_id: str, chat_id: int) -> bool:
    if settings.cooldown_seconds <= 0:
        return False
    doc = await cols.cooldowns.find_one({"_id": f"{rule_id}:{chat_id}"})
    return bool(doc and doc["expire_at"] > now())


async def start_cooldown(rule_id: str, chat_id: int) -> None:
    if settings.cooldown_seconds <= 0:
        return
    await cols.cooldowns.replace_one(
        {"_id": f"{rule_id}:{chat_id}"},
        {"expire_at": now() + timedelta(seconds=settings.cooldown_seconds)},
        upsert=True,
    )


# ---------- monthly reply usage ----------
async def get_usage(user_id: int) -> int:
    doc = await cols.usage.find_one({"user_id": user_id, "month": month_key()})
    return doc["count"] if doc else 0


async def consume_faq_request(user_id: int) -> bool:
    """Atomically use one monthly Smart FAQ request allowance."""
    month = month_key()
    try:
        await cols.usage.update_one(
            {"user_id": user_id, "month": month},
            {"$setOnInsert": {"faq_count": 0}},
            upsert=True,
        )
    except DuplicateKeyError:
        pass

    await cols.usage.update_one(
        {"user_id": user_id, "month": month, "faq_count": {"$exists": False}},
        {"$set": {"faq_count": 0}},
    )
    doc = await cols.usage.find_one_and_update(
        {
            "user_id": user_id,
            "month": month,
            "faq_count": {"$lt": settings.faq_monthly_request_limit},
        },
        {"$inc": {"faq_count": 1}},
        return_document=ReturnDocument.AFTER,
    )
    return doc is not None


async def _flag_once(user_id: int, month: str, flag: str) -> bool:
    res = await cols.usage.update_one(
        {"user_id": user_id, "month": month, flag: False}, {"$set": {flag: True}}
    )
    return res.modified_count == 1


async def consume_reply(user: dict) -> tuple[bool, str | None, int | None]:
    """Atomically take one reply from the owner's allowance.

    Returns (allowed, notice, count) where notice is None, 'limit_near' or 'limit_reached'
    and is returned at most once per month for each kind.
    """
    uid, month, limit = user["_id"], month_key(), reply_limit(user)
    try:
        await cols.usage.update_one(
            {"user_id": uid, "month": month},
            {"$setOnInsert": {"count": 0, "warned_80": False, "warned_limit": False}},
            upsert=True,
        )
    except DuplicateKeyError:
        pass  # concurrent insert, the doc exists now

    flt = {"user_id": uid, "month": month}
    if limit is not None:
        flt["count"] = {"$lt": limit}
    doc = await cols.usage.find_one_and_update(
        flt, {"$inc": {"count": 1}}, return_document=ReturnDocument.AFTER
    )
    if doc is None:
        return False, None, None

    notice = None
    if limit is not None:
        if doc["count"] >= limit:
            if await _flag_once(uid, month, "warned_limit"):
                notice = "limit_reached"
        elif doc["count"] >= math.ceil(limit * 0.8):
            if await _flag_once(uid, month, "warned_80"):
                notice = "limit_near"
    return True, notice, doc["count"]


async def refund_reply(user_id: int) -> None:
    await cols.usage.update_one(
        {"user_id": user_id, "month": month_key(), "count": {"$gt": 0}},
        {"$inc": {"count": -1}},
    )


# ---------- durations ----------
_DUR = re.compile(
    r"^\s*(?:(\d+)\s*d(?:ays?)?)?\s*(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?\s*$",
    re.I,
)


def parse_duration(text: str) -> int | None:
    """'45m', '2h', '1h30m', '1d' -> minutes. None if unreadable."""
    m = _DUR.match(text or "")
    if not m or not any(m.groups()):
        return None
    d, h, mi = (int(x or 0) for x in m.groups())
    return (d * 1440 + h * 60 + mi) or None


def fmt_minutes(total: int) -> str:
    d, rem = divmod(int(total), 1440)
    h, mi = divmod(rem, 60)
    parts = [f"{d}d" if d else "", f"{h}h" if h else "", f"{mi}m" if mi else ""]
    return " ".join(p for p in parts if p) or "0m"


# ---------- follow-up sequences ----------
def followup_used_minutes(rule: dict) -> int:
    return sum(s["delay_min"] for s in rule.get("followups") or [])


def followup_budget_left(rule: dict) -> int:
    return max(0, settings.followup_max_total_minutes - followup_used_minutes(rule))


def conv_key(conn_id: str, chat_id: int) -> str:
    return f"{conn_id}:{chat_id}"


async def start_conversation(owner: dict, rule: dict, conn_id: str, message) -> None:
    """Called right after an auto-reply is sent: schedule follow-ups / owner reminder."""
    if not is_growth(owner):
        return
    steps = rule.get("followups") or []
    notify = rule.get("followup_notify")
    if not steps and not notify:
        return
    t = now()
    sender = message.from_user
    msg_date = message.date if message.date.tzinfo else message.date.replace(tzinfo=timezone.utc)
    doc = {
        "owner_id": owner["_id"],
        "conn_id": conn_id,
        "chat_id": message.chat.id,
        "rule_id": rule["_id"],
        "contact_id": sender.id,
        "contact_name": sender.full_name,
        "contact_username": sender.username,
        "next_step": 0,
        "next_due_at": t + timedelta(minutes=steps[0]["delay_min"]) if steps else None,
        "notify_at": t + timedelta(minutes=notify["delay_min"]) if notify else None,
        "window_ends_at": msg_date + timedelta(hours=24),
        "last_bot_send_at": t,
        "created_at": t,
        "expire_at": t + timedelta(days=10),
    }
    await cols.convs.replace_one({"_id": conv_key(conn_id, message.chat.id)}, doc, upsert=True)


async def cancel_conversation(conn_id: str, chat_id: int) -> None:
    """The contact wrote again: they responded, so the sequence stops."""
    await cols.convs.delete_one({"_id": conv_key(conn_id, chat_id)})


async def cancel_if_manual(conn_id: str, chat_id: int) -> None:
    """The owner wrote in the chat: stop, unless it's just the echo of our own send."""
    key = conv_key(conn_id, chat_id)
    conv = await cols.convs.find_one({"_id": key}, {"last_bot_send_at": 1})
    if not conv:
        return
    last = conv.get("last_bot_send_at")
    if last and (now() - last).total_seconds() < settings.echo_guard_seconds:
        return
    await cols.convs.delete_one({"_id": key})

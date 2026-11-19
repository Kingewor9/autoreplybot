from typing import Any

from pymongo import ASCENDING, AsyncMongoClient

from app.config import settings


class Collections:
    users: Any = None        # _id = Telegram user id; plan, flow state, notification markers
    connections: Any = None  # _id = business_connection_id
    rules: Any = None        # keyword rules per owner
    usage: Any = None        # monthly reply counters
    cooldowns: Any = None    # TTL: (rule, contact) cooldown markers
    payments: Any = None     # Stars payment log (idempotency)
    convs: Any = None        # one pending follow-up sequence per (connection, chat)


cols = Collections()
_client: AsyncMongoClient | None = None


async def init() -> None:
    global _client
    _client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True)
    database = _client[settings.mongodb_db]
    cols.users = database["users"]
    cols.connections = database["connections"]
    cols.rules = database["rules"]
    cols.usage = database["usage"]
    cols.cooldowns = database["cooldowns"]
    cols.payments = database["payments"]
    cols.convs = database["convs"]

    await cols.users.create_index("premium_until")
    await cols.connections.create_index("user_id")
    await cols.rules.create_index([("owner_id", ASCENDING), ("created_at", ASCENDING)])
    await cols.usage.create_index(
        [("user_id", ASCENDING), ("month", ASCENDING)], unique=True
    )
    await cols.cooldowns.create_index("expire_at", expireAfterSeconds=0)
    await cols.payments.create_index("telegram_payment_charge_id", unique=True)
    await cols.convs.create_index("next_due_at")
    await cols.convs.create_index("notify_at")
    await cols.convs.create_index("expire_at", expireAfterSeconds=0)


async def close() -> None:
    if _client is not None:
        await _client.close()

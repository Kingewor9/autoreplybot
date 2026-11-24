import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost/test")

from app.handlers.business import on_business_connection


class BusinessConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_is_notified_only_when_connection_becomes_active(self):
        user = SimpleNamespace(
            id=123,
            full_name="<Customer & Co>",
            username="customer",
        )
        connection = SimpleNamespace(
            id="connection-id",
            user=user,
            user_chat_id=123,
            is_enabled=True,
            rights=SimpleNamespace(can_reply=True),
        )
        previous_connection = {
            "is_enabled": True,
            "can_reply": True,
        }
        connections = SimpleNamespace(
            find_one_and_update=AsyncMock(side_effect=[None, previous_connection])
        )
        bot = SimpleNamespace(send_message=AsyncMock())

        with (
            patch("app.handlers.business.cols.connections", connections),
            patch("app.handlers.business.services.ensure_user", new=AsyncMock()),
            patch(
                "app.handlers.business.settings",
                SimpleNamespace(admin_telegram_id=999),
            ),
            patch("app.handlers.business.services.now", return_value=None),
        ):
            await on_business_connection(connection, bot)
            await on_business_connection(connection, bot)

        admin_messages = [
            call for call in bot.send_message.await_args_list if call.args[0] == 999
        ]
        self.assertEqual(len(admin_messages), 1)
        self.assertIn("&lt;Customer &amp; Co&gt;", admin_messages[0].args[1])
        self.assertIn("@customer", admin_messages[0].args[1])
        self.assertIn("<code>123</code>", admin_messages[0].args[1])


if __name__ == "__main__":
    unittest.main()

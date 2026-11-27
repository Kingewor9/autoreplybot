import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost/test")

from app import ai_faq, services
from app.handlers.business import on_business_message


class SmartFaqTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyword_match_prevents_smart_faq_call(self):
        rule = {"_id": "rule-id"}
        message = SimpleNamespace(
            business_connection_id="connection-id",
            from_user=SimpleNamespace(id=456, is_bot=False),
            chat=SimpleNamespace(id=789),
            text="price?",
            caption=None,
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        connections = SimpleNamespace(
            find_one=AsyncMock(
                return_value={
                    "user_id": 123,
                    "is_enabled": True,
                    "can_reply": True,
                }
            )
        )
        with (
            patch("app.handlers.business.cols.connections", connections),
            patch("app.handlers.business.services.cancel_conversation", new=AsyncMock()),
            patch(
                "app.handlers.business.services.get_user",
                new=AsyncMock(return_value={"_id": 123}),
            ),
            patch(
                "app.handlers.business.services.list_rules",
                new=AsyncMock(return_value=[rule]),
            ),
            patch("app.handlers.business.services.find_match", return_value=rule),
            patch(
                "app.handlers.business.services.on_cooldown",
                new=AsyncMock(return_value=True),
            ),
            patch("app.handlers.business.ai_faq.answer", new=AsyncMock()) as answer,
        ):
            await on_business_message(message, bot)

        answer.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_smart_faq_reply_uses_business_connection_and_free_watermark(self):
        message = SimpleNamespace(
            business_connection_id="connection-id",
            from_user=SimpleNamespace(id=456, is_bot=False),
            chat=SimpleNamespace(id=789),
            text="How much?",
            caption=None,
        )
        bot = SimpleNamespace(send_message=AsyncMock())
        connections = SimpleNamespace(
            find_one=AsyncMock(
                return_value={
                    "user_id": 123,
                    "is_enabled": True,
                    "can_reply": True,
                }
            )
        )
        owner = {"_id": 123, "faq_enabled": True, "faq_text": "Price is ₦10,000."}
        with (
            patch("app.handlers.business.cols.connections", connections),
            patch("app.handlers.business.services.cancel_conversation", new=AsyncMock()),
            patch("app.handlers.business.services.get_user", new=AsyncMock(return_value=owner)),
            patch("app.handlers.business.services.list_rules", new=AsyncMock(return_value=[])),
            patch("app.handlers.business.services.find_match", return_value=None),
            patch("app.handlers.business.services.get_usage", new=AsyncMock(return_value=0)),
            patch(
                "app.handlers.business.services.consume_faq_request",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "app.handlers.business.ai_faq.answer",
                new=AsyncMock(return_value="It costs ₦10,000."),
            ),
            patch(
                "app.handlers.business.services.consume_reply",
                new=AsyncMock(return_value=(True, None, 1)),
            ),
            patch("app.handlers.business.services.is_paid", return_value=False),
        ):
            await on_business_message(message, bot)

        bot.send_message.assert_awaited_once_with(
            chat_id=789,
            text="It costs ₦10,000.\n\nAuto-replied via @Cuezabot",
            business_connection_id="connection-id",
        )

    async def test_gemini_receives_business_info_and_ignore_is_silent(self):
        requests = []

        class FakeResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc_value, _traceback):
                _ = _exc_value, _traceback
                return _exc_type is None

            async def json(self):
                return {
                    "candidates": [
                        {"content": {"parts": [{"text": "IGNORE"}]}}
                    ]
                }

        class FakeSession:
            def __init__(self, **_kwargs):
                self.timeout = _kwargs["timeout"]

            async def __aenter__(self):
                return self

            async def __aexit__(self, _exc_type, _exc_value, _traceback):
                _ = _exc_value, _traceback
                return _exc_type is None

            def post(self, url, **kwargs):
                requests.append({"url": url, **kwargs})
                return FakeResponse()

        with (
            patch(
                "app.ai_faq.settings",
                SimpleNamespace(gemini_api_key="test-key", gemini_model="gemini-test"),
            ),
            patch.object(ai_faq.aiohttp, "ClientSession", FakeSession),
        ):
            result = await ai_faq.answer("Ads cost ₦10,000.", "How much are ads?")

        self.assertIsNone(result)
        self.assertIn("gemini-test:generateContent", requests[0]["url"])
        self.assertEqual(requests[0]["params"], {"key": "test-key"})
        self.assertIn(
            "Ads cost ₦10,000.",
            requests[0]["json"]["system_instruction"]["parts"][0]["text"],
        )
        self.assertEqual(
            requests[0]["json"]["contents"][0]["parts"][0]["text"],
            "How much are ads?",
        )

    async def test_faq_request_allowance_handles_legacy_usage_document(self):
        updates = []
        usage = SimpleNamespace(
            update_one=AsyncMock(
                side_effect=lambda *args, **kwargs: updates.append((args, kwargs))
            ),
            find_one_and_update=AsyncMock(return_value={"faq_count": 1}),
        )
        with (
            patch("app.services.cols.usage", usage),
            patch(
                "app.services.settings",
                SimpleNamespace(faq_monthly_request_limit=1500),
            ),
            patch("app.services.month_key", return_value="2026-11"),
        ):
            allowed = await services.consume_faq_request(123)

        self.assertTrue(allowed)
        usage.find_one_and_update.assert_awaited_once()
        self.assertIn(
            {"$set": {"faq_count": 0}}, [call[0][1] for call in updates]
        )

    async def test_faq_request_allowance_stops_at_monthly_cap(self):
        usage = SimpleNamespace(
            update_one=AsyncMock(),
            find_one_and_update=AsyncMock(return_value=None),
        )
        with (
            patch("app.services.cols.usage", usage),
            patch(
                "app.services.settings",
                SimpleNamespace(faq_monthly_request_limit=1500),
            ),
            patch("app.services.month_key", return_value="2026-11"),
        ):
            allowed = await services.consume_faq_request(123)

        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()

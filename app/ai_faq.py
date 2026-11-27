"""Gemini-backed Smart FAQ answer generation."""
import logging

import aiohttp

from app.config import settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You reply on behalf of a business owner on Telegram.
Use only the business information below to answer the incoming message.
Keep answers warm, natural, and concise. Never invent prices, links, or policies.
If the message is casual personal chat or is not answered by the business information,
respond with only the exact word IGNORE.

BUSINESS INFORMATION:
{faq_text}"""


class SmartFaqError(RuntimeError):
    """The configured AI provider could not produce a usable answer."""


async def answer(faq_text: str, incoming_message: str) -> str | None:
    """Return a grounded answer, or None when the model says to ignore the message."""
    if not settings.gemini_api_key:
        raise SmartFaqError("GEMINI_API_KEY is not configured")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent"
    )
    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT.format(faq_text=faq_text)}]
        },
        "contents": [{"role": "user", "parts": [{"text": incoming_message}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 256},
    }

    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, params={"key": settings.gemini_api_key}, json=payload
            ) as response:
                if response.status >= 400:
                    log.error("Gemini Smart FAQ request failed with HTTP %s", response.status)
                    raise SmartFaqError(f"Gemini returned HTTP {response.status}")
                data = await response.json()
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise SmartFaqError("Gemini Smart FAQ request failed") from exc

    try:
        text = "".join(
            part["text"]
            for part in data["candidates"][0]["content"]["parts"]
            if isinstance(part.get("text"), str)
        ).strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise SmartFaqError("Gemini returned an unexpected response") from exc

    if not text:
        raise SmartFaqError("Gemini returned an empty answer")
    if text.casefold() == "ignore":
        return None
    if len(text) > 4000:
        raise SmartFaqError("Gemini returned an answer that is too long")
    return text

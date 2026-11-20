import html

from aiogram import Bot
from aiogram.types import Message

from app.keyboards import rule_markup

WATERMARK = "\n\nAuto-replied via @Cuezabot"
PHOTO_CAPTION_LIMIT = 1024


async def send_rule_reply(
    bot: Bot,
    chat_id: int,
    rule: dict,
    business_connection_id: str | None = None,
    watermark: bool = False,
) -> Message:
    """Send a rule's saved reply (text or photo+caption, with optional link buttons).

    With business_connection_id the message goes out from the owner's own account;
    without it, it's a plain message from the bot (used for the Preview button).
    """
    kwargs = {"chat_id": chat_id, "reply_markup": rule_markup(rule)}
    if business_connection_id:
        kwargs["business_connection_id"] = business_connection_id

    if rule.get("photo_file_id"):
        caption = rule.get("reply_html") or ""
        if watermark:
            plain_caption = rule.get("reply_plain") or ""
            if len(plain_caption) + len(WATERMARK) > PHOTO_CAPTION_LIMIT:
                plain_caption = plain_caption[: PHOTO_CAPTION_LIMIT - len(WATERMARK)]
                caption = html.escape(plain_caption)
            caption = caption + WATERMARK if caption else WATERMARK.strip()
        return await bot.send_photo(
            photo=rule["photo_file_id"], caption=caption or None, **kwargs
        )
    else:
        text = rule["reply_html"]
        if watermark:
            text += WATERMARK
        return await bot.send_message(text=text, **kwargs)

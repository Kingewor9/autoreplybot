import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from pymongo.errors import DuplicateKeyError

from app import keyboards as kb
from app import services
from app.config import PLAN_NAMES, PLAN_RANK, settings
from app.db import cols
from app.paystack import PaystackError, initialize_transaction, verify_transaction
from app.ui import show

log = logging.getLogger(__name__)
router = Router()

SUBSCRIPTION_PERIOD = 2_592_000  # Telegram requires exactly 30 days (in seconds)
PAID_PLANS = ("pro", "growth")


def _naira_price(plan: str) -> int:
    return settings.pro_naira_price if plan == "pro" else settings.growth_naira_price


def _paystack_enabled() -> bool:
    return bool(settings.paystack_secret_key)


def _paystack_items(plans: tuple[str, ...]) -> list[tuple[str, str]]:
    if not _paystack_enabled():
        return []
    return [
        (
            f"🇳🇬 {PLAN_NAMES[plan]} · ₦{_naira_price(plan):,}",
            f"pay:{plan}:ngn",
        )
        for plan in plans
    ]


def _price(plan: str) -> int:
    return settings.stars_price if plan == "pro" else settings.growth_price


async def _invoice_link(bot: Bot, uid: int, plan: str) -> str:
    name = PLAN_NAMES[plan]
    if plan == "pro":
        desc = (
            f"Up to {settings.paid_rule_limit} auto-reply rules, unlimited replies, "
            "and no 'Auto-replied via @Cuezabot' watermark. Renews monthly."
        )
    else:
        desc = (
            f"Up to {settings.growth_rule_limit} rules, unlimited replies, automatic follow-ups "
            "and owner reminders, with no 'Auto-replied via @Cuezabot' watermark. Renews monthly."
        )
    return await bot.create_invoice_link(
        title=f"{name} plan — 1 month",
        description=desc,
        payload=f"{plan}:{uid}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{name} (monthly)", amount=_price(plan))],
        subscription_period=SUBSCRIPTION_PERIOD,
    )


async def render_subscription(bot: Bot, tg_user):
    user = await services.ensure_user(tg_user)
    uid = user["_id"]
    plan = services.current_plan(user)
    rules_used = await cols.rules.count_documents({"owner_id": uid})
    replies_used = await services.get_usage(uid)
    until = user.get("premium_until")

    pro_price = (
        f"{settings.stars_price} Stars or ₦{settings.pro_naira_price:,}"
        if _paystack_enabled()
        else f"{settings.stars_price} Stars"
    )
    growth_price = (
        f"{settings.growth_price} Stars or ₦{settings.growth_naira_price:,}"
        if _paystack_enabled()
        else f"{settings.growth_price} Stars"
    )
    pro_info = (
        f"⭐ <b>Pro — {pro_price} / month</b>\n"
        f"• Up to {settings.paid_rule_limit} rules\n"
        "• Unlimited auto-replies\n• No “Auto-replied via @Cuezabot” watermark"
    )
    growth_info = (
        f"🚀 <b>Growth — {growth_price} / month</b>\n"
        f"• Up to {settings.growth_rule_limit} rules\n"
        "• Unlimited auto-replies\n• No “Auto-replied via @Cuezabot” watermark\n"
        "• Automatic follow-ups + owner reminders"
    )

    if plan != "free":
        text = (
            "💳 <b>Subscription</b>\n\n"
            f"✅ <b>You're on {PLAN_NAMES[plan]}</b>\n"
            f"Active until <b>{until:%d %b %Y}</b>."
            + (
                " Paystack payments last 30 days and must be renewed manually.\n"
                if user.get("payment_provider") == "paystack"
                else " Renews automatically via Telegram Stars. To cancel renewal: Telegram "
                "<b>Settings → My Stars → Subscriptions</b>.\n"
            )
            + "\n"
            f"📋 Rules: {rules_used}/{services.rule_limit(user)}\n"
            f"💬 Replies this month: {replies_used} (unlimited)"
        )
        if plan == "pro":
            text += (
                f"\n\n{growth_info}\n<i>Upgrading switches you to Growth right away and cancels "
                "your Pro renewal.</i>"
            )
            link = await _invoice_link(bot, uid, "growth")
            stars_items = [
                (f"🚀 Growth · {settings.growth_price} Stars / month", link)
            ]
            paystack_items = _paystack_items(("growth",))
            if user.get("payment_provider") == "paystack":
                pro_link = await _invoice_link(bot, uid, "pro")
                stars_items.append((f"⭐ Renew Pro · {settings.stars_price} Stars", pro_link))
                paystack_items = _paystack_items(("pro", "growth"))
            return text, kb.subscribe_kb(stars_items, paystack_items)
        if user.get("payment_provider") == "paystack":
            link = await _invoice_link(bot, uid, "growth")
            return text, kb.subscribe_kb(
                [(f"🚀 Renew Growth · {settings.growth_price} Stars", link)],
                _paystack_items(("growth",)),
            )
        paystack_items = _paystack_items(("growth",))
        if paystack_items:
            text += "\n\nPaying with Paystack adds 30 days and cancels your Stars renewal."
            return text, kb.subscribe_kb([], paystack_items)
        return text, kb.subscribe_kb([])

    ended = (
        f"Your {PLAN_NAMES.get(user.get('plan') or 'pro', 'Pro')} plan ended on <b>{until:%d %b %Y}</b>.\n\n"
        if until
        else ""
    )
    text = (
        "💳 <b>Subscription</b>\n\n"
        f"{ended}"
        "<b>Free plan</b> (current)\n"
        f"• {settings.free_rule_limit} active rules\n"
        f"• {settings.free_reply_limit} auto-replies / month\n"
        "• Auto-replies include the “Auto-replied via @Cuezabot” watermark\n\n"
        f"{pro_info}\n\n{growth_info}\n\n"
        f"📋 Rules: {rules_used}/{settings.free_rule_limit}\n"
        f"💬 Replies this month: {replies_used}/{settings.free_reply_limit}"
    )
    if _paystack_enabled():
        text += "\n\n🇳🇬 Paystack is a one-time payment for 30 days; renew manually."
    pro_link = await _invoice_link(bot, uid, "pro")
    growth_link = await _invoice_link(bot, uid, "growth")
    return text, kb.subscribe_kb(
        [
            (f"⭐ Pro · {settings.stars_price} Stars / month", pro_link),
            (f"🚀 Growth · {settings.growth_price} Stars / month", growth_link),
        ],
        _paystack_items(("pro", "growth")),
    )


@router.callback_query(F.data == "sub")
async def cb_subscription(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    text, markup = await render_subscription(bot, cb.from_user)
    await show(cb, text, markup)


@router.message(Command("upgrade", "subscribe"), F.chat.type == "private")
async def cmd_subscription(message: Message, bot: Bot):
    text, markup = await render_subscription(bot, message.from_user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("pay:"))
async def cb_paystack_checkout(cb: CallbackQuery, bot: Bot):
    parts = (cb.data or "").split(":")
    if len(parts) != 3 or parts[1] not in PAID_PLANS or parts[2] != "ngn":
        await cb.answer("Invalid payment option.", show_alert=True)
        return
    if not _paystack_enabled():
        await cb.answer(
            "Naira payments are not configured yet. Please use Telegram Stars.",
            show_alert=True,
        )
        return

    await cb.answer()
    plan = parts[1]
    try:
        reference, checkout_url = await initialize_transaction(cb.from_user.id, plan)
    except PaystackError:
        log.exception("Could not initialize Paystack transaction for user %s", cb.from_user.id)
        await bot.send_message(
            cb.from_user.id,
            "Paystack checkout is temporarily unavailable. Please try again or use Telegram Stars.",
        )
        return

    await bot.send_message(
        cb.from_user.id,
        f"Pay ₦{_naira_price(plan):,} for 30 days of {PLAN_NAMES[plan]} access. "
        "After paying, return here and tap “I've Paid” to verify your transaction. "
        "Bank transfers may take a little while to confirm.",
        reply_markup=kb.paystack_checkout_kb(
            reference, _naira_price(plan), checkout_url
        ),
    )


@router.callback_query(F.data.startswith("payv:"))
async def cb_verify_paystack_payment(cb: CallbackQuery, bot: Bot):
    reference = (cb.data or "").partition(":")[2]
    if len(reference) != 32 or any(char not in "0123456789abcdef" for char in reference):
        await cb.answer("Invalid payment reference.", show_alert=True)
        return

    await cb.answer()
    try:
        status, payment = await verify_transaction(reference, cb.from_user.id)
    except PaystackError:
        log.exception("Could not verify Paystack payment for user %s", cb.from_user.id)
        await bot.send_message(
            cb.from_user.id,
            "I couldn't verify that payment right now. Please try again shortly.",
        )
        return

    if status == "pending":
        await bot.send_message(
            cb.from_user.id,
            "Paystack hasn't confirmed this payment yet. If you paid by bank transfer, "
            "wait a little and tap “I've Paid” again.",
        )
    elif status == "completed":
        await bot.send_message(cb.from_user.id, "This payment has already been applied.")
    elif payment is not None:
        await notify_paystack_payment(bot, payment)


async def notify_paystack_payment(
    bot: Bot, payment: tuple[int, str, datetime, str | None]
) -> None:
    user_id, plan, expiry, old_charge = payment
    cancellation_failed = False
    if old_charge:
        try:
            await bot.edit_user_star_subscription(
                user_id=user_id,
                telegram_payment_charge_id=old_charge,
                is_canceled=True,
            )
        except TelegramAPIError:
            cancellation_failed = True
            log.exception("Could not cancel old Stars subscription for %s", user_id)
    renewal_note = (
        " Please cancel your previous Telegram Stars subscription in "
        "<b>Settings → My Stars → Subscriptions</b> to avoid another charge."
        if cancellation_failed
        else ""
    )
    try:
        await bot.send_message(
            user_id,
            f"🎉 Paystack payment received! Your {PLAN_NAMES[plan]} plan is active until "
            f"<b>{expiry:%d %b %Y}</b>.{renewal_note}",
        )
    except TelegramAPIError:
        log.exception("Could not notify user %s about Paystack payment", user_id)


@router.message(Command("paysupport"), F.chat.type == "private")
async def cmd_paysupport(message: Message):
    await message.answer(
        f"💬 Payment problem? Contact {settings.support_contact} and include your "
        "Telegram username and the time of the payment."
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery):
    if (query.invoice_payload or "").split(":")[0] not in PAID_PLANS:
        await query.answer(ok=False, error_message="Unknown product.")
        return
    await query.answer(ok=True)


def _expiry(sp) -> datetime:
    exp = getattr(sp, "subscription_expiration_date", None)
    if isinstance(exp, datetime):
        return exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    return services.now() + timedelta(days=30)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, bot: Bot):
    sp = message.successful_payment
    user = await services.ensure_user(message.from_user)
    uid = user["_id"]
    plan = (sp.invoice_payload or "").split(":")[0]
    if plan not in PAID_PLANS:
        plan = "pro"

    try:  # fires for the first payment AND every monthly renewal
        await cols.payments.insert_one(
            {
                "telegram_payment_charge_id": sp.telegram_payment_charge_id,
                "user_id": uid,
                "plan": plan,
                "amount": sp.total_amount,
                "currency": sp.currency,
                "payload": sp.invoice_payload,
                "created_at": services.now(),
            }
        )
    except DuplicateKeyError:
        return  # already processed

    current = services.current_plan(user)
    if PLAN_RANK[plan] < PLAN_RANK[current]:
        # e.g. a leftover Pro renewal while the user is on Growth: don't downgrade them
        await message.answer(
            f"ℹ️ A <b>{PLAN_NAMES[plan]}</b> payment just went through, but you're already on "
            f"<b>{PLAN_NAMES[current]}</b>. Cancel the extra subscription in Telegram "
            "<b>Settings → My Stars → Subscriptions</b> to avoid being charged again, and contact "
            f"{settings.support_contact} for a refund."
        )
        return

    expiry = _expiry(sp)
    charge_id = sp.telegram_payment_charge_id
    first = bool(getattr(sp, "is_first_recurring", False)) or not getattr(sp, "is_recurring", False)
    old_charge = user.get("subscription_charge_id")
    switched_up = current != "free" and PLAN_RANK[plan] > PLAN_RANK[current]

    update = {"plan": plan, "premium_until": expiry}
    update["payment_provider"] = "stars"
    if first or user.get("plan") != plan or not old_charge:
        update["subscription_charge_id"] = charge_id
    await cols.users.update_one({"_id": uid}, {"$set": update})

    cancelled_old = False
    if switched_up and old_charge and old_charge != charge_id:
        try:  # stop the old subscription from renewing (best effort)
            await bot.edit_user_star_subscription(
                user_id=uid, telegram_payment_charge_id=old_charge, is_canceled=True
            )
            cancelled_old = True
        except TelegramAPIError as e:
            log.warning("Could not cancel old subscription for %s: %s", uid, e)

    renewal = bool(getattr(sp, "is_recurring", False)) and not bool(getattr(sp, "is_first_recurring", False))
    name = PLAN_NAMES[plan]
    if renewal:
        text = f"🔄 <b>{name} renewed.</b> You're covered until <b>{expiry:%d %b %Y}</b>."
    elif switched_up:
        text = (
            f"🚀 <b>Upgraded to {name}!</b> Active until <b>{expiry:%d %b %Y}</b>.\n\n"
            + (
                "Your previous subscription won't renew."
                if cancelled_old
                else "Please cancel your old subscription in Telegram "
                "<b>Settings → My Stars → Subscriptions</b> so it doesn't renew."
            )
        )
    elif plan == "growth":
        text = (
            f"🎉 <b>Welcome to Growth!</b>\n\nActive until <b>{expiry:%d %b %Y}</b>, renewing monthly. "
            f"You can now have up to {settings.growth_rule_limit} rules, unlimited replies, and "
            "follow-ups on every rule (open a rule → ⏱ Follow-ups)."
        )
    else:
        text = (
            f"🎉 <b>Welcome to Pro!</b>\n\nActive until <b>{expiry:%d %b %Y}</b> and renewing monthly.\n"
            f"You can now have up to {settings.paid_rule_limit} rules and unlimited replies."
        )
    await message.answer(text, reply_markup=kb.main_menu())

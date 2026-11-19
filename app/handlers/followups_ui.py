"""Follow-up flows per rule (Growth plan): steps, delays and the owner reminder."""
import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app import keyboards as kb
from app import services
from app.config import settings
from app.db import cols
from app.handlers.rules import BUTTONS_HELP, extract_reply
from app.ui import show

router = Router()
fmt = services.fmt_minutes


# ------------------------------------------------------------------ helpers
def _preview(step: dict) -> str:
    bits = []
    if step.get("photo_file_id"):
        bits.append("🖼")
    plain = (step.get("reply_plain") or "").replace("\n", " ")
    if plain:
        bits.append(html.escape(plain[:60] + ("…" if len(plain) > 60 else "")))
    if step.get("buttons"):
        bits.append(f"🔘×{len(step['buttons'])}")
    return " ".join(bits) or "(empty)"


def render_followups(rule: dict):
    steps = rule.get("followups") or []
    lines = [
        f"⏱ <b>Follow-ups · {html.escape(rule['keyword'])}</b>\n",
        "If the contact doesn't reply, I send these from your account — each one after the "
        "previous message. They stop the moment the contact <i>or you</i> reply.\n",
    ]
    if steps:
        for i, s in enumerate(steps, 1):
            lines.append(f"{i}. after <b>{fmt(s['delay_min'])}</b> — {_preview(s)}")
    else:
        lines.append("<i>No follow-ups yet.</i>")
    lines.append(
        f"\n🕒 Time used: {fmt(services.followup_used_minutes(rule))} of "
        f"{fmt(settings.followup_max_total_minutes)} (Telegram's 24-hour window)"
    )
    notify = rule.get("followup_notify")
    lines.append("🔔 Owner reminder: " + (f"after <b>{fmt(notify['delay_min'])}</b>" if notify else "off"))
    can_add = (
        len(steps) < settings.followup_max_steps
        and services.followup_budget_left(rule) >= settings.followup_min_minutes
    )
    return "\n".join(lines), kb.followups_kb(str(rule["_id"]), len(steps), can_add)


def render_notify(rule: dict):
    notify = rule.get("followup_notify")
    now_txt = f"after <b>{fmt(notify['delay_min'])}</b>" if notify else "<b>off</b>"
    text = (
        f"🔔 <b>Owner reminder · {html.escape(rule['keyword'])}</b>\n\n"
        "If the contact still hasn't replied after the time you choose (counted from the "
        "auto-reply), I'll message you here so you can follow up manually. This can be "
        f"longer than 24 hours, up to {fmt(settings.notify_max_minutes)}.\n\n"
        f"Currently: {now_txt}"
    )
    return text, kb.notify_kb(str(rule["_id"]), bool(notify))


def _delay_error(rule: dict, minutes: int | None) -> str | None:
    if minutes is None:
        return "I couldn't read that. Try something like <code>45m</code>, <code>2h</code> or <code>1h30m</code>."
    if minutes < settings.followup_min_minutes:
        return f"The minimum gap is {settings.followup_min_minutes} minutes."
    left = services.followup_budget_left(rule)
    if minutes > left:
        return f"That doesn't fit — you have <b>{fmt(left)}</b> left in the 24-hour window."
    return None


async def _send(target, text: str, markup=None) -> None:
    if isinstance(target, CallbackQuery):
        await show(target, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _ctx(cb: CallbackQuery, rule_id: str):
    await cb.answer()
    user = await services.ensure_user(cb.from_user)
    return user, await services.get_rule(rule_id, user["_id"])


async def _gate(cb: CallbackQuery, user: dict, rule: dict | None) -> bool:
    if not rule:
        await show(cb, "That rule no longer exists.", kb.empty_rules_kb())
        return False
    if not services.is_growth(user):
        await show(
            cb,
            "⏱ <b>Follow-ups are part of the Growth plan</b>\n\n"
            f"🚀 {settings.growth_price} Stars / month: up to {settings.growth_rule_limit} rules, "
            "unlimited replies, automatic follow-ups when a contact goes quiet, and owner "
            "reminders to follow up manually.",
            kb.upsell_kb(str(rule["_id"])),
        )
        return False
    return True


async def _show_followups(target, user_id: int, rule_id: str, prefix: str = "") -> None:
    rule = await services.get_rule(rule_id, user_id)
    if rule:
        text, markup = render_followups(rule)
        await _send(target, prefix + text, markup)


# ------------------------------------------------------------------ screens & step adding
@router.callback_query(F.data.startswith("fu:"))
async def cb_followups(cb: CallbackQuery):
    user, rule = await _ctx(cb, cb.data.split(":", 1)[1])
    await services.set_flow(user["_id"], None)
    if not await _gate(cb, user, rule):
        return
    text, markup = render_followups(rule)
    await show(cb, text, markup)


@router.callback_query(F.data.startswith("fua:"))
async def cb_add_step(cb: CallbackQuery):
    rule_id = cb.data.split(":", 1)[1]
    user, rule = await _ctx(cb, rule_id)
    if not await _gate(cb, user, rule):
        return
    left = services.followup_budget_left(rule)
    if len(rule.get("followups") or []) >= settings.followup_max_steps or left < settings.followup_min_minutes:
        await _show_followups(cb, user["_id"], rule_id)
        return
    await services.set_flow(user["_id"], {"step": "fu_delay", "mode": "fu", "rule_id": rule_id})
    await show(
        cb,
        f"⏱ <b>When should this follow-up go out?</b>\n\nCounted from the previous message "
        f"(your auto-reply, or the previous follow-up). You have <b>{fmt(left)}</b> left in the "
        "24-hour window.\n\nTap a time or type your own, e.g. <code>45m</code>, <code>2h</code>, "
        "<code>1h30m</code>.",
        kb.delay_kb(rule_id, left),
    )


async def _ask_reply(target, user_id: int, rule_id: str, minutes: int) -> None:
    await services.set_flow(
        user_id,
        {"step": "fu_reply", "mode": "fu", "rule_id": rule_id, "draft": {"delay_min": minutes}},
    )
    await _send(
        target,
        f"✅ After <b>{fmt(minutes)}</b>.\n\n💬 Now send the <b>follow-up message</b> — text, or a "
        "photo with an optional caption.",
        kb.cancel_kb(),
    )


@router.callback_query(F.data.startswith("fud:"))
async def cb_pick_delay(cb: CallbackQuery):
    _, rule_id, minutes = cb.data.split(":")
    user, rule = await _ctx(cb, rule_id)
    if not await _gate(cb, user, rule):
        return
    err = _delay_error(rule, int(minutes))
    if err:
        await cb.message.answer(err)
        return
    await _ask_reply(cb, user["_id"], rule_id, int(minutes))


async def _save_step(target, user: dict, rule_id: str, draft: dict) -> None:
    rule = await services.get_rule(rule_id, user["_id"])
    uid = user["_id"]
    if not rule or len(rule.get("followups") or []) >= settings.followup_max_steps or _delay_error(rule, draft["delay_min"]):
        await services.set_flow(uid, None)
        await _send(target, "Couldn't add that step (limit reached or rule changed).", kb.main_menu())
        return
    step = {
        "delay_min": draft["delay_min"],
        "reply_html": draft.get("reply_html", ""),
        "reply_plain": draft.get("reply_plain", ""),
        "photo_file_id": draft.get("photo_file_id"),
        "buttons": draft.get("buttons", []),
    }
    await cols.rules.update_one({"_id": rule["_id"]}, {"$push": {"followups": step}})
    await services.set_flow(uid, None)
    await _show_followups(target, uid, rule_id, prefix="✅ Follow-up added.\n\n")


@router.callback_query(F.data == "fuskip")
async def cb_skip_buttons(cb: CallbackQuery):
    await cb.answer()
    user = await services.ensure_user(cb.from_user)
    flow = user.get("flow")
    if not flow or flow.get("step") != "fu_buttons":
        await cb.message.answer("This step has expired.")
        return
    await _save_step(cb, user, flow["rule_id"], flow["draft"])


@router.callback_query(F.data.startswith("fudl:"))
async def cb_delete_step(cb: CallbackQuery):
    _, rule_id, idx = cb.data.split(":")
    user, rule = await _ctx(cb, rule_id)
    if not await _gate(cb, user, rule):
        return
    steps = list(rule.get("followups") or [])
    if 0 <= int(idx) < len(steps):
        steps.pop(int(idx))
        await cols.rules.update_one({"_id": rule["_id"]}, {"$set": {"followups": steps}})
    await _show_followups(cb, user["_id"], rule_id)


# ------------------------------------------------------------------ owner reminder
@router.callback_query(F.data.startswith("fn:"))
async def cb_notify(cb: CallbackQuery):
    user, rule = await _ctx(cb, cb.data.split(":", 1)[1])
    await services.set_flow(user["_id"], None)
    if not await _gate(cb, user, rule):
        return
    text, markup = render_notify(rule)
    await show(cb, text, markup)


async def _set_notify(target, user: dict, rule_id: str, minutes: int | None) -> None:
    update = {"$set": {"followup_notify": {"delay_min": minutes}}} if minutes else {"$unset": {"followup_notify": ""}}
    rule = await services.get_rule(rule_id, user["_id"])
    if not rule:
        return
    await cols.rules.update_one({"_id": rule["_id"]}, update)
    await services.set_flow(user["_id"], None)
    rule = await services.get_rule(rule_id, user["_id"])
    text, markup = render_notify(rule)
    await _send(target, "✅ Saved.\n\n" + text, markup)


@router.callback_query(F.data.startswith("fnd:"))
async def cb_notify_pick(cb: CallbackQuery):
    _, rule_id, minutes = cb.data.split(":")
    user, rule = await _ctx(cb, rule_id)
    if await _gate(cb, user, rule):
        await _set_notify(cb, user, rule_id, int(minutes))


@router.callback_query(F.data.startswith("fnoff:"))
async def cb_notify_off(cb: CallbackQuery):
    user, rule = await _ctx(cb, cb.data.split(":", 1)[1])
    if await _gate(cb, user, rule):
        await _set_notify(cb, user, str(rule["_id"]), None)


@router.callback_query(F.data.startswith("fnc:"))
async def cb_notify_custom(cb: CallbackQuery):
    rule_id = cb.data.split(":", 1)[1]
    user, rule = await _ctx(cb, rule_id)
    if not await _gate(cb, user, rule):
        return
    await services.set_flow(user["_id"], {"step": "fn_delay", "mode": "fn", "rule_id": rule_id})
    await show(
        cb,
        "✏️ After how long without a reply should I remind you?\n\nType e.g. "
        "<code>12h</code>, <code>2d</code> or <code>1d 6h</code>.",
        kb.cancel_kb(),
    )


# ------------------------------------------------------------------ typed input for these flows
async def _in_followup_flow(message: Message):
    if not message.from_user:
        return False
    user = await services.get_user(message.from_user.id)
    flow = (user or {}).get("flow")
    if flow and flow.get("mode") in ("fu", "fn"):
        return {"user": user, "flow": flow}
    return False


@router.message(F.chat.type == "private", _in_followup_flow)
async def on_followup_input(message: Message, user: dict, flow: dict):
    uid, step, rule_id = user["_id"], flow["step"], flow["rule_id"]
    rule = await services.get_rule(rule_id, uid)
    if not rule or not services.is_growth(user):
        await services.set_flow(uid, None)
        await message.answer("That's no longer available.", reply_markup=kb.main_menu())
        return
    if message.text and message.text.startswith("/"):
        await message.answer("Finish this step first, or tap Cancel.", reply_markup=kb.cancel_kb())
        return

    if step == "fu_delay":
        minutes = services.parse_duration(message.text or "")
        err = _delay_error(rule, minutes)
        if err:
            await message.answer(err, reply_markup=kb.delay_kb(rule_id, services.followup_budget_left(rule)))
            return
        await _ask_reply(message, uid, rule_id, minutes)

    elif step == "fu_reply":
        data, err = extract_reply(message)
        if err:
            await message.answer(err, reply_markup=kb.cancel_kb())
            return
        draft = {**flow["draft"], **data}
        await services.set_flow(uid, {"step": "fu_buttons", "mode": "fu", "rule_id": rule_id, "draft": draft})
        await message.answer(
            BUTTONS_HELP.format(n=settings.max_buttons) + "\n\nOr skip this step.",
            reply_markup=kb.fu_buttons_kb(),
        )

    elif step == "fu_buttons":
        buttons = services.parse_buttons(message.text or "")
        if buttons is None:
            await message.answer(
                "I couldn't read that. Use one button per line:\n<code>Label - https://link.com</code>",
                reply_markup=kb.fu_buttons_kb(),
            )
            return
        await _save_step(message, user, rule_id, {**flow["draft"], "buttons": buttons})

    elif step == "fn_delay":
        minutes = services.parse_duration(message.text or "")
        if minutes is None or minutes < settings.followup_min_minutes or minutes > settings.notify_max_minutes:
            await message.answer(
                f"Send a time between {fmt(settings.followup_min_minutes)} and "
                f"{fmt(settings.notify_max_minutes)}, e.g. <code>12h</code> or <code>2d</code>.",
                reply_markup=kb.cancel_kb(),
            )
            return
        await _set_notify(message, user, rule_id, minutes)

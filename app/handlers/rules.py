"""Rule management (add / list / edit / delete).

The multi-step conversations are tracked with a small `flow` object stored on the
user document, so they survive restarts and work with several server instances.
"""
import html

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from app import keyboards as kb
from app import services
from app.config import settings
from app.db import cols
from app.sender import send_rule_reply
from app.ui import show

router = Router()

MATCH_LABEL = {"exact": "Exact match", "contains": "Contains keyword"}
BUTTONS_HELP = (
    "🔘 Send up to <b>{n}</b> link buttons, one per line:\n"
    "<code>Label - https://link.com</code>\n\n"
    "Example:\n<code>Order now - https://example.com/order\n"
    "Chat with us - https://t.me/yourname</code>"
)
REPLY_HELP = (
    "💬 Now send the <b>reply message</b>.\n\n"
    "• Text — bold, links and emojis are kept\n"
    "• Or a <b>photo</b> (with an optional caption)"
)


def _short(text: str, n: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def summary(rule: dict, active: bool = True) -> str:
    lines = [
        f"🔑 <b>{html.escape(rule['keyword'])}</b>",
        f"🎯 {MATCH_LABEL[rule['match_type']]}",
    ]
    if rule.get("photo_file_id"):
        lines.append("🖼 Image attached")
    if rule.get("reply_plain"):
        lines.append(f"💬 {html.escape(_short(rule['reply_plain'], 150))}")
    if rule.get("buttons"):
        lines.append("🔘 " + ", ".join(html.escape(b["text"]) for b in rule["buttons"]))
    if rule.get("followups"):
        lines.append(f"⏱ {len(rule['followups'])} follow-up step(s)")
    if rule.get("followup_notify"):
        lines.append(f"🔔 Owner reminder after {services.fmt_minutes(rule['followup_notify']['delay_min'])}")
    if not active:
        lines.append("⏸ <i>Inactive on your current plan — upgrade to reactivate.</i>")
    return "\n".join(lines)


def extract_reply(message: Message) -> tuple[dict | None, str | None]:
    if message.photo:
        caption = message.caption or ""
        if len(caption) > 1024:
            return None, "That caption is too long (max 1024 characters). Please shorten it and resend."
        return {
            "photo_file_id": message.photo[-1].file_id,
            "reply_html": message.html_text if caption else "",
            "reply_plain": caption,
        }, None
    if message.text:
        if len(message.text) > 4000:
            return None, "That message is too long (max 4000 characters)."
        return {
            "photo_file_id": None,
            "reply_html": message.html_text,
            "reply_plain": message.text,
        }, None
    return None, "Please send text or a photo (with an optional caption)."


async def render_rules(user: dict):
    rules = await services.list_rules(user["_id"])
    if not rules:
        return "📋 <b>My Rules</b>\n\nYou don't have any rules yet.", kb.empty_rules_kb()
    limit = services.rule_limit(user)
    lines = [f"📋 <b>My Rules</b> ({len(rules)}/{limit})\n"]
    for i, r in enumerate(rules, 1):
        mark = "✅" if i <= limit else "⏸"
        lines.append(f"{mark} <b>{html.escape(r['keyword'])}</b> · {MATCH_LABEL[r['match_type']]}")
    if len(rules) > limit:
        lines.append("\n⏸ Paused rules are saved — they reactivate when you upgrade.")
    return "\n".join(lines), kb.rules_list_kb(rules)


async def render_edit(user: dict, rule: dict):
    rules = await services.list_rules(user["_id"])
    idx = next((i for i, r in enumerate(rules) if r["_id"] == rule["_id"]), 0)
    active = idx < services.rule_limit(user)
    return "✏️ <b>Edit rule</b>\n\n" + summary(rule, active), kb.edit_kb(str(rule["_id"]))


async def _own_rule(cb: CallbackQuery, rule_id: str):
    user = await services.ensure_user(cb.from_user)
    rule = await services.get_rule(rule_id, user["_id"])
    return user, rule


# ------------------------------------------------------------------ list / add
@router.callback_query(F.data == "rules")
async def cb_rules(cb: CallbackQuery):
    user = await services.ensure_user(cb.from_user)
    await services.set_flow(user["_id"], None)
    await cb.answer()
    text, markup = await render_rules(user)
    await show(cb, text, markup)


@router.callback_query(F.data == "add")
async def cb_add(cb: CallbackQuery):
    user = await services.ensure_user(cb.from_user)
    await cb.answer()
    limit = services.rule_limit(user)
    count = await cols.rules.count_documents({"owner_id": user["_id"]})
    if count >= limit:
        plan = services.current_plan(user)
        text = f"You've reached your limit of <b>{limit}</b> rules. "
        if plan == "growth":
            text += "Delete a rule to add a new one."
        elif plan == "pro":
            text += f"Delete one, or upgrade to Growth for up to <b>{settings.growth_rule_limit}</b> rules."
        else:
            text += (
                f"Delete one, or upgrade: Pro gives <b>{settings.paid_rule_limit}</b> rules, "
                f"Growth gives <b>{settings.growth_rule_limit}</b>."
            )
        await show(cb, text, kb.limit_kb(plan != "growth"))
        return
    await services.set_flow(user["_id"], {"step": "keyword", "mode": "create", "draft": {}})
    await show(
        cb,
        "➕ <b>New rule</b>\n\nSend the <b>trigger keyword</b> (e.g. <i>Ads</i>).",
        kb.cancel_kb(),
    )


@router.callback_query(F.data.in_({"m:exact", "m:contains"}))
async def cb_match(cb: CallbackQuery):
    user = await services.ensure_user(cb.from_user)
    flow = user.get("flow")
    if not flow or flow.get("step") != "match":
        await cb.answer("This step has expired. Start again from the menu.", show_alert=True)
        return
    await cb.answer()
    draft = flow["draft"]
    draft["match_type"] = cb.data.split(":")[1]
    await services.set_flow(user["_id"], {"step": "reply", "mode": "create", "draft": draft})
    await show(cb, REPLY_HELP, kb.cancel_kb())


@router.callback_query(F.data == "skipbtn")
async def cb_skip_buttons(cb: CallbackQuery):
    user = await services.ensure_user(cb.from_user)
    flow = user.get("flow")
    if not flow or flow.get("step") != "buttons":
        await cb.answer("This step has expired.", show_alert=True)
        return
    await cb.answer()
    if flow["mode"] == "create":
        await _save_new(cb.message, user, flow["draft"], via_cb=cb)
    else:
        await cols.rules.update_one({"_id": _oid(flow["rule_id"])}, {"$set": {"buttons": []}})
        await _finish_edit(user, flow["rule_id"], cb=cb)


def _oid(rule_id: str):
    from bson import ObjectId

    return ObjectId(rule_id)


async def _save_new(message, user, draft, via_cb: CallbackQuery | None = None):
    draft.setdefault("buttons", [])
    rule, err = await services.create_rule(user, draft)
    await services.set_flow(user["_id"], None)
    if err == "limit":
        text, markup = "You've reached your rule limit for this plan.", kb.limit_kb(services.current_plan(user) != "growth")
    elif err == "duplicate":
        text, markup = (
            "You already have a rule with this keyword and match type. "
            "Edit that one from <b>My Rules</b> instead.",
            kb.empty_rules_kb(),
        )
    else:
        text, markup = "✅ <b>Rule saved!</b>\n\n" + summary(rule), kb.after_save_kb(str(rule["_id"]))
    if via_cb:
        await show(via_cb, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


async def _finish_edit(user, rule_id: str, cb: CallbackQuery | None = None, message: Message | None = None):
    await services.set_flow(user["_id"], None)
    rule = await services.get_rule(rule_id, user["_id"])
    if not rule:
        return
    text, markup = await render_edit(user, rule)
    text = "✅ Updated.\n\n" + text
    if cb:
        await show(cb, text, markup)
    else:
        await message.answer(text, reply_markup=markup)


# ------------------------------------------------------------------ edit
@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(cb: CallbackQuery):
    user, rule = await _own_rule(cb, cb.data.split(":", 1)[1])
    await services.set_flow(user["_id"], None)
    await cb.answer()
    if not rule:
        await show(cb, "That rule no longer exists.", kb.empty_rules_kb())
        return
    text, markup = await render_edit(user, rule)
    await show(cb, text, markup)


@router.callback_query(F.data.regexp(r"^e[krb]:"))
async def cb_edit_field(cb: CallbackQuery):
    kind, rule_id = cb.data.split(":", 1)
    user, rule = await _own_rule(cb, rule_id)
    await cb.answer()
    if not rule:
        await show(cb, "That rule no longer exists.", kb.empty_rules_kb())
        return
    step = {"ek": "keyword", "er": "reply", "eb": "buttons"}[kind]
    await services.set_flow(user["_id"], {"step": step, "mode": "edit", "rule_id": rule_id})
    if step == "keyword":
        await show(cb, "🔑 Send the <b>new keyword</b>.", kb.cancel_kb())
    elif step == "reply":
        await show(cb, REPLY_HELP.replace("Now send", "Send the new"), kb.cancel_kb())
    else:
        await show(cb, BUTTONS_HELP.format(n=settings.max_buttons), kb.buttons_step_kb(editing=True))


@router.callback_query(F.data.startswith("em:"))
async def cb_edit_match(cb: CallbackQuery):
    rule_id = cb.data.split(":", 1)[1]
    _, rule = await _own_rule(cb, rule_id)
    await cb.answer()
    if not rule:
        await show(cb, "That rule no longer exists.", kb.empty_rules_kb())
        return
    await show(cb, "🎯 Choose how this keyword should match:", kb.match_kb(rule_id))


@router.callback_query(F.data.startswith("sm:"))
async def cb_set_match(cb: CallbackQuery):
    _, rule_id, match_type = cb.data.split(":")
    user, rule = await _own_rule(cb, rule_id)
    if not rule or match_type not in MATCH_LABEL:
        await cb.answer("Rule not found.", show_alert=True)
        return
    if await services.rule_exists(user["_id"], rule["keyword_norm"], match_type, exclude_id=rule["_id"]):
        await cb.answer("You already have a rule with this keyword and match type.", show_alert=True)
        return
    await cb.answer()
    await cols.rules.update_one({"_id": rule["_id"]}, {"$set": {"match_type": match_type}})
    await _finish_edit(user, rule_id, cb=cb)


@router.callback_query(F.data.startswith("pv:"))
async def cb_preview(cb: CallbackQuery, bot: Bot):
    _, rule = await _own_rule(cb, cb.data.split(":", 1)[1])
    if not rule:
        await cb.answer("Rule not found.", show_alert=True)
        return
    await cb.answer("Preview sent below 👇")
    await send_rule_reply(bot, cb.from_user.id, rule)


# ------------------------------------------------------------------ delete
@router.callback_query(F.data.startswith("del:"))
async def cb_delete(cb: CallbackQuery):
    _, rule = await _own_rule(cb, cb.data.split(":", 1)[1])
    await cb.answer()
    if not rule:
        await show(cb, "That rule no longer exists.", kb.empty_rules_kb())
        return
    await show(
        cb,
        f"Delete the rule <b>{html.escape(rule['keyword'])}</b>? This can't be undone.",
        kb.confirm_delete_kb(str(rule["_id"])),
    )


@router.callback_query(F.data.startswith("delc:"))
async def cb_delete_confirm(cb: CallbackQuery):
    user, rule = await _own_rule(cb, cb.data.split(":", 1)[1])
    await cb.answer("Deleted" if rule else "Already deleted")
    if rule:
        await cols.rules.delete_one({"_id": rule["_id"]})
    text, markup = await render_rules(user)
    await show(cb, text, markup)


# ------------------------------------------------------------------ conversation steps
@router.message(F.chat.type == "private")
async def on_private_message(message: Message):
    user = await services.ensure_user(message.from_user)
    flow = user.get("flow")
    uid = user["_id"]

    if not flow or (message.text and message.text.startswith("/")):
        await message.answer("Use the menu below 👇", reply_markup=kb.main_menu())
        return

    step, mode = flow["step"], flow["mode"]

    if step == "match":
        await message.answer("Please tap <b>Exact match</b> or <b>Contains</b> above 👆")
        return

    if step == "keyword":
        keyword = (message.text or "").strip()
        norm = services.normalize(keyword)
        if not norm or len(keyword) > 64:
            await message.answer("Send a keyword as text (1–64 characters).", reply_markup=kb.cancel_kb())
            return
        if mode == "create":
            draft = {"keyword": keyword, "keyword_norm": norm}
            await services.set_flow(uid, {"step": "match", "mode": "create", "draft": draft})
            await message.answer(
                f"How should I match <b>{html.escape(keyword)}</b>?\n\n"
                "🎯 <b>Exact match</b> — the whole message must equal the keyword\n"
                "🔍 <b>Contains</b> — the keyword appears anywhere as a word in the message\n\n"
                "<i>Matching ignores upper/lower case.</i>",
                reply_markup=kb.match_kb(),
            )
        else:
            rule = await services.get_rule(flow["rule_id"], uid)
            if not rule:
                await services.set_flow(uid, None)
                await message.answer("That rule no longer exists.", reply_markup=kb.empty_rules_kb())
                return
            if await services.rule_exists(uid, norm, rule["match_type"], exclude_id=rule["_id"]):
                await message.answer("You already have a rule with that keyword. Send a different one.", reply_markup=kb.cancel_kb())
                return
            await cols.rules.update_one(
                {"_id": rule["_id"]}, {"$set": {"keyword": keyword, "keyword_norm": norm}}
            )
            await _finish_edit(user, flow["rule_id"], message=message)
        return

    if step == "reply":
        data, err = extract_reply(message)
        if err:
            await message.answer(err, reply_markup=kb.cancel_kb())
            return
        if mode == "create":
            draft = {**flow["draft"], **data}
            await services.set_flow(uid, {"step": "buttons", "mode": "create", "draft": draft})
            await message.answer(
                BUTTONS_HELP.format(n=settings.max_buttons) + "\n\nOr skip this step.",
                reply_markup=kb.buttons_step_kb(editing=False),
            )
        else:
            await cols.rules.update_one({"_id": _oid(flow["rule_id"])}, {"$set": data})
            await _finish_edit(user, flow["rule_id"], message=message)
        return

    if step == "buttons":
        buttons = services.parse_buttons(message.text or "")
        if buttons is None:
            await message.answer(
                "I couldn't read that. Use one button per line:\n"
                "<code>Label - https://link.com</code>\n"
                f"(max {settings.max_buttons}, links must start with https://)",
                reply_markup=kb.buttons_step_kb(editing=(mode == "edit")),
            )
            return
        if mode == "create":
            await _save_new(message, user, {**flow["draft"], "buttons": buttons})
        else:
            await cols.rules.update_one({"_id": _oid(flow["rule_id"])}, {"$set": {"buttons": buttons}})
            await _finish_edit(user, flow["rule_id"], message=message)

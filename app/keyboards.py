from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Markup


def _kb(*rows: list[Btn]) -> Markup:
    return Markup(inline_keyboard=[list(r) for r in rows])


def _short(text: str, n: int = 16) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def main_menu() -> Markup:
    return _kb(
        [Btn(text="➕ Add New Rule", callback_data="add")],
        [Btn(text="📋 My Rules", callback_data="rules")],
        [Btn(text="💳 Subscription / Upgrade", callback_data="sub")],
    )


def back_menu() -> Markup:
    return _kb([Btn(text="⬅️ Menu", callback_data="menu")])


def cancel_kb() -> Markup:
    return _kb([Btn(text="✖️ Cancel", callback_data="cancel")])


def match_kb(rule_id: str | None = None) -> Markup:
    prefix = f"sm:{rule_id}:" if rule_id else "m:"
    return _kb(
        [
            Btn(text="🎯 Exact match", callback_data=f"{prefix}exact"),
            Btn(text="🔍 Contains", callback_data=f"{prefix}contains"),
        ],
        [Btn(text="✖️ Cancel", callback_data="cancel")],
    )


def buttons_step_kb(editing: bool) -> Markup:
    label = "🗑 Remove buttons" if editing else "⏭ Skip"
    return _kb(
        [Btn(text=label, callback_data="skipbtn")],
        [Btn(text="✖️ Cancel", callback_data="cancel")],
    )


def empty_rules_kb() -> Markup:
    return _kb(
        [Btn(text="➕ Add New Rule", callback_data="add")],
        [Btn(text="⬅️ Menu", callback_data="menu")],
    )


def rules_list_kb(rules: list[dict]) -> Markup:
    rows = []
    for r in rules:
        rid, kw = str(r["_id"]), _short(r["keyword"])
        rows.append(
            [
                Btn(text=f"✏️ Edit · {kw}", callback_data=f"edit:{rid}"),
                Btn(text="🗑 Delete", callback_data=f"del:{rid}"),
            ]
        )
    rows.append([Btn(text="➕ Add New Rule", callback_data="add")])
    rows.append([Btn(text="⬅️ Menu", callback_data="menu")])
    return _kb(*rows)


def edit_kb(rule_id: str) -> Markup:
    return _kb(
        [
            Btn(text="🔑 Keyword", callback_data=f"ek:{rule_id}"),
            Btn(text="🎯 Match type", callback_data=f"em:{rule_id}"),
        ],
        [
            Btn(text="💬 Reply", callback_data=f"er:{rule_id}"),
            Btn(text="🔘 Buttons", callback_data=f"eb:{rule_id}"),
        ],
        [Btn(text="⏱ Follow-ups", callback_data=f"fu:{rule_id}")],
        [Btn(text="👁 Preview", callback_data=f"pv:{rule_id}")],
        [Btn(text="⬅️ Back to rules", callback_data="rules")],
    )


def confirm_delete_kb(rule_id: str) -> Markup:
    return _kb(
        [
            Btn(text="🗑 Yes, delete", callback_data=f"delc:{rule_id}"),
            Btn(text="↩️ Keep it", callback_data="rules"),
        ]
    )


def after_save_kb(rule_id: str) -> Markup:
    return _kb(
        [Btn(text="👁 Preview", callback_data=f"pv:{rule_id}")],
        [Btn(text="➕ Add another", callback_data="add")],
        [Btn(text="📋 My Rules", callback_data="rules")],
    )


def limit_kb(can_upgrade: bool) -> Markup:
    rows = [[Btn(text="📋 My Rules", callback_data="rules")]]
    if can_upgrade:
        rows.insert(0, [Btn(text="💳 Upgrade", callback_data="sub")])
    return _kb(*rows)


def upgrade_kb() -> Markup:
    return _kb([Btn(text="💳 Upgrade", callback_data="sub")])


def subscribe_kb(
    items: list[tuple[str, str]], paystack_items: list[tuple[str, str]] | None = None
) -> Markup:
    """items: (button label, invoice link); paystack_items: (label, callback data)."""
    rows = [[Btn(text=label, url=url)] for label, url in items]
    rows.extend(
        [Btn(text=label, callback_data=callback_data)]
        for label, callback_data in (paystack_items or [])
    )
    rows.append([Btn(text="⬅️ Menu", callback_data="menu")])
    return _kb(*rows)


def paystack_checkout_kb(reference: str, amount: int, checkout_url: str) -> Markup:
    return _kb(
        [Btn(text=f"💳 Pay Now (₦{amount:,})", url=checkout_url)],
        [Btn(text="✅ I've Paid", callback_data=f"payv:{reference}")],
        [Btn(text="⬅️ Menu", callback_data="menu")],
    )


def rule_markup(rule: dict) -> Markup | None:
    """The link buttons attached to the auto-reply itself."""
    buttons = rule.get("buttons") or []
    if not buttons:
        return None
    return Markup(inline_keyboard=[[Btn(text=b["text"], url=b["url"])] for b in buttons])


# ---------------- follow-ups ----------------
def upsell_kb(rule_id: str) -> Markup:
    return _kb(
        [Btn(text="🚀 See the Growth plan", callback_data="sub")],
        [Btn(text="⬅️ Back to rule", callback_data=f"edit:{rule_id}")],
    )


def followups_kb(rule_id: str, n_steps: int, can_add: bool) -> Markup:
    rows = []
    if can_add:
        rows.append([Btn(text="➕ Add follow-up", callback_data=f"fua:{rule_id}")])
    for i in range(n_steps):
        rows.append([Btn(text=f"🗑 Remove step {i + 1}", callback_data=f"fudl:{rule_id}:{i}")])
    rows.append([Btn(text="🔔 Owner reminder", callback_data=f"fn:{rule_id}")])
    rows.append([Btn(text="⬅️ Back to rule", callback_data=f"edit:{rule_id}")])
    return _kb(*rows)


def delay_kb(rule_id: str, left_minutes: int) -> Markup:
    options = [(m, label) for m, label in ((30, "30m"), (60, "1h"), (180, "3h"), (360, "6h"), (720, "12h")) if m <= left_minutes]
    rows = []
    if options:
        rows.append([Btn(text=label, callback_data=f"fud:{rule_id}:{m}") for m, label in options])
    rows.append([Btn(text="⬅️ Back", callback_data=f"fu:{rule_id}")])
    return _kb(*rows)


def fu_buttons_kb() -> Markup:
    return _kb(
        [Btn(text="⏭ Skip", callback_data="fuskip")],
        [Btn(text="✖️ Cancel", callback_data="cancel")],
    )


def notify_kb(rule_id: str, enabled: bool) -> Markup:
    rows = [
        [
            Btn(text=label, callback_data=f"fnd:{rule_id}:{m}")
            for m, label in ((360, "6h"), (1440, "24h"), (2880, "48h"), (4320, "72h"))
        ],
        [Btn(text="✏️ Custom time", callback_data=f"fnc:{rule_id}")],
    ]
    if enabled:
        rows.append([Btn(text="🔕 Turn off", callback_data=f"fnoff:{rule_id}")])
    rows.append([Btn(text="⬅️ Back", callback_data=f"fu:{rule_id}")])
    return _kb(*rows)


def open_chat_kb(url: str) -> Markup:
    return _kb([Btn(text="💬 Open chat", url=url)])

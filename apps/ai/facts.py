"""The business's facts, structured: what an automatic reply may state, in a form code can check.

The prompt gets these as JSON next to the owner's free-text notes, and ``apps.ai.autonomy`` checks
an automatic reply against them. A price is accepted because it *is* a catalogue price (or an
amount the owner wrote in the notes), a time because it *is* an opening or closing time, not
because the same digits happen to appear somewhere.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

MAX_PRODUCTS = 30

# "K18,000", "ZMW 250", "$12.50", "250 kwacha", "1,200 USD"
_CURRENCY = r"(?:K|ZMW|USD|US\$|\$|KES|KSh|NGN|₦|R|ZAR|GHS|£|€|TZS|UGX|MWK)"
MONEY = re.compile(
    rf"(?<![\w.]){_CURRENCY}\s?(\d[\d,]*(?:\.\d+)?)(?!\w)"
    r"|(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(?:kwacha|dollars?|zmw|usd|kes|ngn|zar)\b",
    re.I,
)
# "08:00", "8am", "5 pm", "17:30"
TIME = re.compile(r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s?(am|pm)\b|(?<!\d)(\d{1,2}):(\d{2})(?!\d)", re.I)


def amount(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def amounts_in(text: str) -> set:
    return {a for m in MONEY.finditer(text or "") if (a := amount(m.group(1) or m.group(2))) is not None}


def minutes_of(match) -> int | None:
    if match.group(4) is not None:
        hours, minutes = int(match.group(4)), int(match.group(5))
    else:
        hours, minutes = int(match.group(1)), int(match.group(2) or 0)
        suffix = match.group(3).lower()
        if hours > 12:
            return None
        hours = hours % 12 + (12 if suffix == "pm" else 0)
    return hours * 60 + minutes if hours < 24 and minutes < 60 else None


def times_in(text: str) -> set:
    return {t for m in TIME.finditer(text or "") if (t := minutes_of(m)) is not None}


def build(account, *, business_notes: str = "") -> dict:
    """``{"opening_hours", "timezone", "products", "business", "notes"}`` for this business.

    ``business`` is the owner's profile answers (location, payment methods, delivery, website).
    Products only when Commerce is on.
    """
    from apps.accounts import api as accounts_api
    from apps.accounts import business_hours
    from apps.billing import api as billing_api

    hours = business_hours.get_hours(account)
    products = []
    if billing_api.usable(account, "orders"):
        from apps.core.actions import ActionError, run_action

        try:
            products = run_action("lookup_products", {"account": account}, account=account, query="*",
                                  limit=MAX_PRODUCTS).get("products", [])
        except ActionError:
            products = []
    return {
        "opening_hours": dict(hours.schedule) if hours and hours.schedule else {},
        "timezone": hours.timezone if hours and hours.schedule else "",
        "products": products,
        "business": accounts_api.business_facts(account),
        "notes": business_notes or "",
    }


def written_text(facts: dict) -> str:
    """Everything the owner wrote themselves (notes and profile answers), for text-level checks."""
    return "\n".join([facts.get("notes", ""), *[str(v) for v in (facts.get("business") or {}).values()]])


def allowed_amounts(facts: dict, extra_text: str = "") -> set:
    """Prices a reply may quote: catalogue prices, amounts in the owner's notes or a look-up."""
    out = {a for p in facts.get("products", []) if (a := amount(str(p.get("price", "")))) is not None}
    return out | amounts_in(written_text(facts)) | amounts_in(extra_text) | {
        a for a in (amount(v) for v in re.findall(r'"price":\s*"([\d.]+)"', extra_text or "")) if a is not None}


def allowed_times(facts: dict, extra_text: str = "") -> set:
    """Times a reply may state: opening and closing times, and times in the notes or a look-up."""
    out = set()
    for window in (facts.get("opening_hours") or {}).values():
        out |= times_in(f"{window.get('open', '')} {window.get('close', '')}")
    return out | times_in(written_text(facts)) | times_in(extra_text)

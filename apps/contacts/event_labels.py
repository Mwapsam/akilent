"""Plain-language labels for ContactEvent rows shown on the customer profile page.

``ContactEvent.type`` is a free-form string set by whichever app recorded the
event (crm, commerce, email, conversations, or a custom name via the ingest
API) — see the call sites in apps/{crm,commerce,logs,events}/services.py. This
module is the one place that translates those into a sentence a non-technical
business owner can read, per the R1 rule: never show a raw type string or a
JSON blob on a page a business user works from (see docs/plans amendment).

Unknown types (custom event names from the public API) fall back to a
title-cased version of the name rather than failing — this list will always be
incomplete, and that's fine as long as it degrades gracefully.
"""

_EXACT = {
    "lead.created": "Became a lead",
    "deal.created": "A deal was opened",
    "deal.stage_changed": "Deal stage changed",
    "order.created": "Placed an order",
    "order.paid": "Order paid",
    "payment.succeeded": "Payment received",
    "payment.failed": "Payment failed",
    "conversation.message_received": "Sent a message",
    "email.delivered": "Email delivered",
    "email.opened": "Opened an email",
    "email.clicked": "Clicked a link in an email",
    "email.bounced": "Email bounced",
    "email.complained": "Marked an email as spam",
    "email.unsubscribed": "Unsubscribed from email",
}


def event_label(event_type: str) -> str:
    """One short, readable label for a ContactEvent's ``type``."""
    if event_type in _EXACT:
        return _EXACT[event_type]
    # Fall back to a readable guess: "foo.bar_baz" -> "Foo bar baz".
    words = event_type.replace(".", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:] if words else "Activity"


def event_detail(event_type: str, data: dict | None) -> str:
    """One optional supporting line, built from ``data`` where it's informative.

    Returns "" when there's nothing worth showing — callers should hide the
    line rather than print an empty sentence.
    """
    data = data or {}
    if event_type == "deal.stage_changed" and data.get("to_stage"):
        return f"Moved to {data['to_stage']}"
    if event_type in ("order.created", "order.paid") and data.get("amount"):
        return f"Amount: {data['amount']}"
    if event_type == "payment.succeeded" and data.get("amount"):
        return f"Amount: {data['amount']}"
    if event_type == "email.clicked" and data.get("url"):
        return data["url"]
    return ""

"""The prompt, built in layers so it stays small and predictable.

System (product rules) -> Business (owner's notes, hours, approved templates) -> Customer snapshot
(first name, tags, whether they're tracked as interested) -> Conversation (the last few messages).
Never the whole history.

Privacy: only what is needed. Phone numbers and email addresses inside message text are masked,
internal notes and automated system lines are never included, and nothing from any other
conversation or customer is sent.
"""
from __future__ import annotations

import re

from apps.ai.types import ChatMessage

RECENT_MESSAGES = 8
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")

SYSTEM_RULES = """You help a small business answer its customers on WhatsApp. You never send anything: \
you propose what a person on the team should send, and they decide.

Rules:
- Use ONLY the facts in "About the business" and "About this customer". If the answer needs a fact \
that isn't there (a price, a date, stock, a policy), don't guess: say a teammate will confirm, or \
propose a handoff.
- Never invent prices, discounts, delivery times or promises.
- Write like a friendly person from the business: short, plain, in the customer's language. No \
markdown, no lists unless the customer asked for options.
- If the reply window is CLOSED, you may only propose one of the approved templates, or a handoff.

Answer with ONE JSON object and nothing else:
{"version": 1, "action": "reply" | "send_template" | "handoff", "confidence": 0.0-1.0,
 "reason": "<one short sentence for the team>", "payload": {...}}

payload for "reply": {"text": "<the message>"}
payload for "send_template": {"template": "<exact approved template name>", "variables": \
{"<blank>": "<value>"}}. Fill EVERY blank listed for that template. For a name blank use \
"contact.first_name". For the business name use "account.company_name". Otherwise use short text \
taken from the facts above. If you can't fill a blank from the facts, propose a handoff instead.
payload for "handoff": {"note": "<what the teammate should know>"}"""


def mask(text: str) -> str:
    """Hide phone numbers and email addresses a customer typed; the model doesn't need them."""
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", text or ""))


def build(*, business_name: str, business_notes: str, hours_text: str, templates: list[dict],
          customer: dict, thread: list[dict], window_open: bool) -> tuple[str, list[ChatMessage]]:
    """``(system, messages)`` ready for ``AIProvider.chat``.

    ``thread`` is the recent conversation, oldest first, as ``{"direction", "body"}``; only inbound
    and outbound entries are used. ``templates`` is the approved templates as ``{"name", "body",
    "blanks"}``.
    """
    lines = [SYSTEM_RULES, "", "## About the business", f"Name: {business_name or 'the business'}"]
    if hours_text:
        lines.append(f"Opening hours: {hours_text}")
    lines.append(business_notes.strip() if business_notes.strip() else "(The owner hasn't added any facts yet.)")

    lines += ["", "## About this customer", f"First name: {customer.get('first_name') or 'unknown'}"]
    if customer.get("tags"):
        lines.append("Tags: " + ", ".join(customer["tags"]))
    if customer.get("interested"):
        lines.append("They are being tracked as an interested customer.")

    lines += ["", f"## Reply window: {'OPEN' if window_open else 'CLOSED'}"]
    if templates:
        lines += ["", "## Approved templates"]
        for t in templates:
            lines.append(f"- {t['name']} (blanks: {', '.join(t['blanks']) or 'none'}): {t['body'][:300]}")

    messages = []
    for entry in thread[-RECENT_MESSAGES:]:
        body = mask(entry.get("body") or "").strip()
        if not body:
            continue
        if entry.get("direction") == "inbound":
            messages.append(ChatMessage("user", body))
        elif entry.get("direction") == "outbound":
            messages.append(ChatMessage("assistant", body))
    if not messages or messages[-1].role != "user":
        messages.append(ChatMessage("user", "(Propose the best next message for the team to send.)"))
    return "\n".join(lines), messages

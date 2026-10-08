"""The prompt, built in layers so it stays small and predictable.

System (product rules) -> Business (owner's notes, hours, approved templates, tags) -> Customer
snapshot (first name, tags, whether they're tracked as interested, remembered facts) -> Conversation
(a rolling summary of earlier messages, see ``apps.ai.memory``, then the last few word for word).
Never the whole history. Anything else the model needs it asks for through ``apps.ai.tools``.

Privacy: only what is needed. Phone numbers and email addresses inside message text are masked,
internal notes and automated system lines are never included, and nothing from any other
conversation or customer is sent.
"""

from __future__ import annotations

import json
import re

from apps.ai.types import ChatMessage

RECENT_MESSAGES = 8
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")

CHANNEL_LABELS = {"whatsapp": "WhatsApp", "instagram": "Instagram"}
SENSITIVE_TOPICS = (
    "complaints, refunds, payment disputes, cancellations, account restrictions, legal threats, "
    "custom pricing or deals, and service disputes"
)
_PROFILE_LABELS = {
    "what_you_sell": "What we offer",
    "location": "Where we are",
    "payment_methods": "How to pay",
    "delivery": "Delivery",
    "website": "Website",
}


def system_rules(business_name: str, channel_label: str = "") -> str:
    """The product rules, spoken from the business's side so the model never confuses whose
    customer this is (it answers *for* the business, never for Akilent)."""
    name = business_name or "the business"
    where = f" on {channel_label}" if channel_label else ""
    return f"""You reply to customers on behalf of {name}{where}. Speak as the business using "we", "our" and "us". Never mention Akilent, the AI system, the software running you, or another company as the business you represent. Your job is to help customers of {name}. The information under "Business knowledge" belongs to {name} and is its source of truth. You never send anything yourself: you propose what the team should send.

Rules:
- If anything under "Business knowledge" answers the customer, even in different words, answer it fully and helpfully in your own words, and list the ids of the entries you used in "sources".
- Use ONLY "Business knowledge" and "About this customer". If the answer needs a fact that isn't there (a price, a date, stock, a policy), don't guess: propose a handoff.
- Quote prices, times, numbers and links exactly as written under "Exact facts" or in the text you used. Never invent prices, discounts, delivery times or promises.
- Sensitive topics ({SENSITIVE_TOPICS}): you may explain a policy written in the business knowledge, but never promise or commit to an outcome (no "we'll refund you", "we'll cancel it"); use intent "sensitive".
- Write like a friendly person from the business: short, plain, in the customer's language. No markdown, no lists unless the customer asked for options.
- If the reply window is CLOSED, you may only propose one of the approved templates, or a handoff.

Answer with ONE JSON object and nothing else:
{{"version": 1, "action": "reply" | "send_template" | "handoff", "confidence": 0.0-1.0,
 "intent": "greeting" | "hours" | "location" | "product_info" | "price" | "delivery" | "payment" | "faq" | "sensitive" | "other",
 "sources": ["<id of each Questions and answers entry you used, e.g. k12>"],
 "reason": "<one short sentence for the team>", "payload": {{...}}}}

intent is what the customer's latest message is mainly about: "faq" when a Questions and answers entry answers it, "sensitive" for the sensitive topics above, "other" when nothing fits. confidence is how sure you are that your proposal is correct and complete using only the business knowledge given; when unsure, give a low confidence rather than a vague answer.

payload for "reply": {{"text": "<the message>"}}
payload for "send_template": {{"template": "<exact approved template name>", "variables": {{"<blank>": "<value>"}}}}. Fill EVERY blank listed for that template. For a name blank use "contact.first_name". For the business name use "account.company_name". Otherwise use short text taken from the business knowledge. If you can't fill a blank from it, propose a handoff instead.
payload for "handoff": {{"note": "<what the teammate should know>"}}

Optionally add "extras": up to 3 small next steps the team can apply with one click. Only when clearly useful:
- {{"kind": "tag", "tag": "<one of the business's tags listed below>"}}
- {{"kind": "track_interest"}} when the customer shows real buying interest and isn't tracked yet
- {{"kind": "follow_up", "in_days": 1-14, "note": "<what to check back on>"}} when something is left open (a quote, a decision, a delivery)"""


def mask(text: str) -> str:
    """Hide phone numbers and email addresses a customer typed; the model doesn't need them."""
    return _PHONE.sub("[phone]", _EMAIL.sub("[email]", text or ""))


def build(
    *,
    business_name: str,
    business_notes: str,
    hours_text: str,
    templates: list[dict],
    customer: dict,
    thread: list[dict],
    window_open: bool,
    memory=None,
    tools_text: str = "",
    business_tags=(),
    structured_facts: dict | None = None,
    channel: str = "",
) -> tuple[str, list[ChatMessage]]:
    """``(system, messages)`` ready for ``AIProvider.chat``.

    ``thread`` is the recent conversation, oldest first, as ``{"direction", "body"}``; only inbound
    and outbound entries are used. ``templates`` is the approved templates as ``{"name", "body",
    "blanks"}``. ``memory`` is ``{"summary", "facts"}`` for the part of the thread before ``thread``.
    ``tools_text`` describes the look-ups the model may ask for. ``channel`` is the conversation's
    channel ("whatsapp", "instagram"...), named in the rules.

    Business knowledge comes in three parts: Questions and answers (explained in the model's own
    words, each with the id it cites), Business information (the owner's notes and profile
    answers), and Exact facts (hours, products and prices, quoted as written).
    """
    facts = structured_facts or {}
    lines = [
        system_rules(business_name, CHANNEL_LABELS.get(channel, "")),
        "",
        "## Business knowledge",
    ]
    if facts.get("knowledge"):
        lines += ["", "### Questions and answers"]
        for entry in facts["knowledge"]:
            lines += [
                f"[{entry['id']}] Q: {entry['title']}",
                f"A: {entry['content']}",
            ]
    lines += [
        "",
        "### Business information",
        f"Name: {business_name or 'the business'}",
    ]
    for key, label in _PROFILE_LABELS.items():
        if (facts.get("business") or {}).get(key):
            lines.append(f"{label}: {facts['business'][key]}")
    if business_notes.strip():
        lines.append(business_notes.strip())
    elif not facts.get("knowledge") and not facts.get("business"):
        lines.append("(The owner hasn't added any facts yet.)")
    exact = {
        k: v
        for k, v in facts.items()
        if k not in ("notes", "knowledge", "business") and v
    }
    if hours_text or exact:
        lines += ["", "### Exact facts (quote prices, times and links only as written)"]
        if hours_text:
            lines.append(f"Opening hours: {hours_text}")
        if exact:
            lines.append(json.dumps(exact, ensure_ascii=False))

    lines += [
        "",
        "## About this customer",
        f"First name: {customer.get('first_name') or 'unknown'}",
    ]
    if customer.get("tags"):
        lines.append("Tags: " + ", ".join(customer["tags"]))
    if customer.get("interested"):
        lines.append("They are being tracked as an interested customer.")
    if memory and memory.get("facts"):
        lines += [f"{k}: {v}" for k, v in memory["facts"].items()]

    if memory and memory.get("summary"):
        lines += ["", "## Earlier in this conversation", memory["summary"]]

    lines += ["", f"## Reply window: {'OPEN' if window_open else 'CLOSED'}"]
    if templates:
        lines += ["", "## Approved templates"]
        for t in templates:
            lines.append(
                f"- {t['name']} (blanks: {', '.join(t['blanks']) or 'none'}): {t['body'][:300]}"
            )
    if business_tags:
        lines += ["", "## The business's tags", ", ".join(business_tags)]
    if tools_text:
        lines += ["", tools_text]

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
        messages.append(
            ChatMessage("user", "(Propose the best next message for the team to send.)")
        )
    return "\n".join(lines), messages

"""One-click engagement follow-ups.

These are the relationship-maintenance jobs a busy owner means when they say
"I'll get back to them" and then doesn't: check in when someone goes quiet,
check back after they showed interest, say thank you. Each installs as a
complete, published workflow — not a draft in an editor — because a starter
that leaves you inside a step editor hasn't saved anyone any work.

Deliberately built from primitives that already exist (``wait``, ``branch`` on
the ``last_engaged_days`` segment field, ``send_whatsapp``), so there is no new
engine machinery to keep correct. Two properties matter and both come from
those primitives:

* **It stops when the customer replies.** Every starter's branch asks whether
  the customer has been quiet for the whole waiting period; an inbound message
  stamps ``Contact.last_engaged_at`` (apps.contacts.services), so a customer
  who wrote back yesterday is never chased today.
* **One customer, one run.** ``enroll`` is a no-op while a run is already
  active for the contact, so five messages in a row do not become five
  check-ins.

Each starter names a *suggested* starter template (apps.whatsapp.starter_templates)
rather than a fixed WhatsApp template name: the owner chooses which of their
own approved templates to send, because only Meta-approved templates can be
sent and only the owner knows which of theirs fits.
"""
from __future__ import annotations

_DAY = 86400

ENGAGEMENT_STARTERS = [
    {
        "key": "welcome-new-enquiry",
        "name": "Welcome every new enquiry",
        "goal": "Nobody who messages you for the first time is left waiting in silence.",
        "explain": (
            "The moment someone messages your WhatsApp number for the first "
            "time, send them a warm welcome."
        ),
        "stop_condition": "Sent once per customer, and only to people who messaged you first.",
        "trigger": "contact.created",
        "welcome": True,
        "suggested_template": "welcome_new_customer",
    },
    {
        "key": "quiet-customer-check-in",
        "name": "Check in when a customer goes quiet",
        "goal": "Nobody who asked you something gets forgotten.",
        "explain": (
            "Three days after a customer messages you, if you still haven't "
            "heard from them, send a short check-in."
        ),
        "stop_condition": "Nothing is sent if the customer writes back first.",
        "trigger": "conversation.message_received",
        "quiet_days": 3,
        "suggested_template": "checking_in",
    },
    {
        "key": "interested-customer-check-back",
        "name": "Check back after someone shows interest",
        "goal": "Interest turns into a sale more often when someone follows up.",
        "explain": (
            "Two days after a customer is tracked as interested, if they've "
            "gone quiet, ask whether they still want to go ahead."
        ),
        "stop_condition": "Nothing is sent if the customer writes back first.",
        "trigger": "lead.created",
        "quiet_days": 2,
        "suggested_template": "still_interested",
    },
    {
        "key": "thank-you-after-a-conversation",
        "name": "Say thank you after a conversation",
        "goal": "A customer who feels looked after comes back.",
        "explain": (
            "A day after a conversation ends, thank the customer for getting "
            "in touch."
        ),
        "stop_condition": "Nothing is sent while the conversation is still going.",
        "trigger": "conversation.message_received",
        "quiet_days": 1,
        "suggested_template": "thank_you",
    },
]

# Keyword auto-replies. Unlike the follow-ups above these need no approved template: the
# customer has just written, so a plain message is allowed (WhatsApp's 24-hour window). The
# owner supplies the answer; the keywords are a starting point they can edit afterwards.
_REPLY_STARTERS = [
    {
        "key": "answer-pricing-questions",
        "name": "Answer pricing questions",
        "goal": "Someone asking the price gets an answer straight away, not tomorrow.",
        "explain": "When a message asks about price, cost or pricing, reply with your prices.",
        "stop_condition": "Answers the same customer at most once an hour.",
        "mode": "contains",
        "keywords": ["price", "prices", "pricing", "cost", "how much"],
        "tag": "pricing-enquiry",
        "reply_hint": "Our prices start at K50. Tell us what you need and we'll send an exact quote.",
    },
    {
        "key": "answer-where-are-you",
        "name": "Answer location questions",
        "goal": "Customers find you without waiting for someone to reply.",
        "explain": "When a message asks where you are, reply with your address and directions.",
        "stop_condition": "Answers the same customer at most once an hour.",
        "mode": "contains",
        "keywords": ["where", "location", "address", "directions"],
        "tag": "location-enquiry",
        "reply_hint": "We're at Plot 12, Cairo Road, Lusaka. Open 8am to 5pm, Monday to Saturday.",
    },
    {
        "key": "greet-hello",
        "name": "Greet people who say hello",
        "goal": "Nobody who says hi is met with silence.",
        "explain": "When a message starts with hello, hi or hey, send a friendly greeting.",
        "stop_condition": "Answers the same customer at most once an hour.",
        "mode": "starts_with",
        "keywords": ["hello", "hi", "hey", "good morning", "good afternoon"],
        "reply_hint": "Hi {first_name}, thanks for getting in touch. How can we help?",
    },
]
for _starter in _REPLY_STARTERS:
    _starter.update({
        "trigger": "conversation.message_received",
        "reply": True,
        "suggested_template": "",
    })
ENGAGEMENT_STARTERS.extend(_REPLY_STARTERS)

STARTERS_BY_KEY = {s["key"]: s for s in ENGAGEMENT_STARTERS}


def build_reply_definition(starter: dict, *, text: str) -> dict:
    """A keyword auto-reply: when the message matches, answer once, tag the customer if the
    starter names a tag (so the owner can later see who asked about prices), then stop."""
    tag = starter.get("tag")
    steps = [{"id": "reply", "type": "reply_text", "text": text, "next": "tag" if tag else "stop"}]
    if tag:
        steps.append({"id": "tag", "type": "add_tag", "tag": tag, "next": "stop"})
    steps.append({"id": "stop", "type": "stop"})
    return {
        "trigger": {
            "type": starter["trigger"],
            "match": {"mode": starter["mode"], "any": list(starter["keywords"])},
            "cooldown_minutes": 60,
        },
        "steps": steps,
    }


def build_definition(starter: dict, *, template_name: str, variable_mapping: dict | None = None) -> dict:
    """Turn a starter plus the owner's chosen template into a workflow definition.

    The shape is always the same: wait out the quiet period, check the customer
    really has been quiet, then send. The branch is what makes this safe to turn
    on — without it the workflow would chase customers mid-conversation.
    """
    if starter.get("welcome"):
        return _welcome_definition(starter, template_name, variable_mapping)
    days = starter["quiet_days"]
    return {
        "trigger": {"type": starter["trigger"]},
        "steps": [
            {"id": "wait", "type": "wait", "seconds": days * _DAY, "next": "still_quiet"},
            {
                "id": "still_quiet", "type": "branch",
                "field": "last_engaged_days", "operator": "gte", "value": days,
                "on_true": "send", "on_false": "stop",
            },
            {
                "id": "send", "type": "send_whatsapp",
                "template": template_name,
                "variable_mapping": variable_mapping or {},
                "next": "stop",
            },
            {"id": "stop", "type": "stop"},
        ],
    }


def _welcome_definition(starter: dict, template_name: str, variable_mapping: dict | None) -> dict:
    """Welcome a brand-new WhatsApp enquirer immediately, once.

    ``contact.created`` fires once per customer, so the welcome is never repeated. The branch
    on ``source`` keeps it to people who messaged first: a contact imported from a CSV or
    added by email has not opted in to WhatsApp and must not receive a template.
    """
    return {
        "trigger": {"type": starter["trigger"]},
        "steps": [
            {
                "id": "messaged_first", "type": "branch",
                "field": "source", "operator": "eq", "value": "whatsapp",
                "on_true": "send", "on_false": "stop",
            },
            {
                "id": "send", "type": "send_whatsapp",
                "template": template_name,
                "variable_mapping": variable_mapping or {},
                "next": "stop",
            },
            {"id": "stop", "type": "stop"},
        ],
    }

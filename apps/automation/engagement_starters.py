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
_REPLY_STARTERS.append({
    "key": "route-new-leads",
    "name": "Hand new interested customers to your team",
    "goal": "A customer who shows interest is never left sitting until someone notices.",
    "explain": (
        "When someone is tracked as interested, give their conversation to your least busy "
        "teammate and email the team so they follow up straight away."
    ),
    "stop_condition": "Nobody who already has a teammate looking after them is reassigned.",
    "trigger": "lead.created",
    "team": True,
    "reply_hint": "{contact} is interested. Please reply to them soon.",
})
_REPLY_STARTERS.append({
    "key": "offer-a-menu",
    "name": "Offer a menu when someone says hello",
    "goal": "Customers get to what they need in one tap, without waiting for a person.",
    "explain": (
        "When someone says hello, ask what they need and offer up to three buttons. "
        "Each button gets its own answer. Use this instead of the plain greeting."
    ),
    "stop_condition": "Asks the same customer at most once an hour, and waits a day for their tap.",
    "menu": True,
    "hello_words": ["hello", "hi", "hey", "good morning", "good afternoon"],
})
_REPLY_STARTERS.append({
    "key": "reply-when-closed",
    "name": "Reply when you're closed",
    "goal": "Customers who write after hours know when to expect an answer.",
    "explain": "When a message arrives outside your opening hours, tell the customer when you'll reply.",
    "stop_condition": "Tells the same customer at most once every 8 hours.",
    "after_hours": True,
    "reply_hint": "Thanks for getting in touch. We're closed right now and will reply when we open at 8am.",
    "needs_hours": True,
})
for _starter in _REPLY_STARTERS:
    _starter.setdefault("trigger", "conversation.message_received")
    _starter.update({"reply": True, "suggested_template": ""})
ENGAGEMENT_STARTERS.extend(_REPLY_STARTERS)

STARTERS_BY_KEY = {s["key"]: s for s in ENGAGEMENT_STARTERS}

# How the Automations home groups them: by what the owner wants help with, not by mechanism.
GOAL_GROUPS = (
    ("Answer customer questions", (
        "answer-pricing-questions", "answer-where-are-you", "welcome-new-enquiry",
        "greet-hello", "offer-a-menu",
    )),
    ("Never miss a customer", (
        "interested-customer-check-back", "quiet-customer-check-in",
        "thank-you-after-a-conversation", "reply-when-closed",
    )),
    ("Keep your team informed", ("route-new-leads",)),
)
assert {k for _, keys in GOAL_GROUPS for k in keys} == set(STARTERS_BY_KEY), "every starter belongs to a goal"


def build_reply_definition(starter: dict, *, text: str, keywords: list[str] | None = None,
                           tag: bool = True, notify: bool = False) -> dict:
    """A one-message auto-reply, then stop.

    Keyword starters answer when the message matches, and tag the customer if the starter
    names a tag (so the owner can later see who asked about prices). The after-hours starter
    answers only while the business is closed, and at most once per customer per 8 hours.
    """
    if starter.get("after_hours"):
        return {
            "trigger": {"type": starter["trigger"], "cooldown_minutes": 480},
            "steps": [
                {"id": "open_now", "type": "branch", "field": "within_business_hours",
                 "operator": "eq", "value": True, "on_true": "stop", "on_false": "reply"},
                {"id": "reply", "type": "reply_text", "text": text, "next": "stop"},
                {"id": "stop", "type": "stop"},
            ],
        }
    tag_name = starter.get("tag") if tag else None
    chain = [("reply", {"type": "reply_text", "text": text})]
    if tag_name:
        chain.append(("tag", {"type": "add_tag", "tag": tag_name}))
    if notify:
        chain.append(("tell", {
            "type": "notify_team", "to": "owners",
            "text": "{contact} sent a message that " + starter["name"] + " answered. Take a look if you'd like to follow up.",
        }))
    steps = []
    for n, (sid, body) in enumerate(chain):
        steps.append({"id": sid, **body, "next": chain[n + 1][0] if n + 1 < len(chain) else "stop"})
    steps.append({"id": "stop", "type": "stop"})
    return {
        "trigger": {
            "type": starter["trigger"],
            "match": {"mode": starter["mode"], "any": list(keywords or starter["keywords"])},
            "cooldown_minutes": 60,
        },
        "steps": steps,
    }


def build_team_definition(starter: dict, *, text: str, notify: str = "assignee", assign: bool = True) -> dict:
    """New lead -> (give it to the least busy teammate) -> tell the team."""
    steps = []
    if assign:
        steps.append({"id": "assign", "type": "assign_conversation", "next": "tell"})
    steps.append({"id": "tell", "type": "notify_team", "to": notify, "text": text, "next": "stop"})
    steps.append({"id": "stop", "type": "stop"})
    return {"trigger": {"type": starter["trigger"]}, "steps": steps}


def build_menu_definition(starter: dict, *, question: str, options: list[dict]) -> dict:
    """A guided menu: greet with buttons, wait for the tap, answer it, and tag what they chose.

    ``options`` is ``[{"title": "Prices", "reply": "Prices start at K50."}, ...]`` (1 to 3).
    Each choice is tagged ``asked-<choice>`` so the owner can later see what customers want.
    """
    from django.utils.text import slugify

    steps = [
        {"id": "ask", "type": "send_buttons", "text": question,
         "buttons": [{"title": o["title"]} for o in options], "next": "wait"},
    ]
    routes = {}
    answers = []
    for n, option in enumerate(options, start=1):
        key = slugify(option["title"])[:200]
        routes[key] = f"answer_{n}"
        answers.append({"id": f"answer_{n}", "type": "reply_text", "text": option["reply"], "next": f"tag_{n}"})
        answers.append({"id": f"tag_{n}", "type": "add_tag",
                        "tag": f"asked-{slugify(option['title'])}"[:40], "next": "stop"})
    steps.append({"id": "wait", "type": "wait_for_reply", "timeout_seconds": 86400,
                  "routes": routes, "on_timeout": "stop"})
    steps.extend(answers)
    steps.append({"id": "stop", "type": "stop"})
    return {
        "trigger": {
            "type": starter.get("trigger", "conversation.message_received"),
            "match": {"mode": "starts_with", "any": list(starter["hello_words"])},
            "cooldown_minutes": 60,
        },
        "steps": steps,
    }


def build_definition(starter: dict, *, template_name: str, variable_mapping: dict | None = None,
                     variable_fallbacks: dict | None = None) -> dict:
    """Turn a starter plus the owner's chosen template into a workflow definition.

    The shape is always the same: wait out the quiet period, check the customer
    really has been quiet, then send. The branch is what makes this safe to turn
    on — without it the workflow would chase customers mid-conversation.
    """
    if starter.get("welcome"):
        return _welcome_definition(starter, template_name, variable_mapping, variable_fallbacks)
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
                "variable_fallbacks": variable_fallbacks or {},
                "next": "stop",
            },
            {"id": "stop", "type": "stop"},
        ],
    }


def _welcome_definition(starter: dict, template_name: str, variable_mapping: dict | None,
                        variable_fallbacks: dict | None = None) -> dict:
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
                "variable_fallbacks": variable_fallbacks or {},
                "next": "stop",
            },
            {"id": "stop", "type": "stop"},
        ],
    }

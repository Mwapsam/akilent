"""What the workflow builder offers this business, and why anything it can't use is greyed out.

The editor never decides on its own whether a step is usable. It asks here, so the palette
can show every step with a plain reason ("Needs WhatsApp connected") instead of hiding
options and leaving the owner guessing why something is missing. The engine and
``validate_definition`` remain the authority on what runs.
"""

from __future__ import annotations

from apps.automation.labels import STEP_LABELS, TRIGGER_LABELS

# (group, [(type, description), ...]) in the order the palette shows them.
_STEP_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Message the customer",
        (
            ("reply_text", "Send a message in the conversation they started."),
            ("send_whatsapp", "Send an approved WhatsApp template, any time."),
            ("send_email", "Send an email."),
        ),
    ),
    (
        "Ask",
        (
            ("send_buttons", "Up to 3 tap-to-answer buttons."),
            ("send_list", "A menu of up to 10 choices (WhatsApp only)."),
            ("wait_for_reply", "Wait for their choice and go the matching way."),
            (
                "ask_question",
                "Ask a question and save the answer on the customer or lead.",
            ),
            ("start_form", "Send one of your forms and wait until they finish it."),
        ),
    ),
    (
        "Wait and decide",
        (
            ("wait", "Pause for minutes, hours or days."),
            ("branch", "Go one way or another based on the customer."),
        ),
    ),
    (
        "Keep track",
        (
            ("add_tag", "Label the customer, e.g. 'asked-price'."),
            ("remove_tag", "Take a label off."),
            ("create_lead", "Track them as an interested customer."),
            ("update_lead_status", "Mark them contacted, qualified or lost."),
            ("set_attribute", "Save a value on the customer's record."),
        ),
    ),
    (
        "Involve your team",
        (
            ("assign_conversation", "Give the conversation to a teammate."),
            ("notify_team", "Email your team about this customer."),
            (
                "create_followup",
                "Set a follow-up for your team. Nothing is sent to the customer.",
            ),
        ),
    ),
    (
        "Advanced",
        (
            ("lookup_product", "Find a product from your catalogue in their message."),
            ("publish_post", "Publish a scheduled Content Studio post."),
            ("webhook", "Send the details to another system."),
            ("action", "Run any Akilent action."),
        ),
    ),
    (
        "With AI (optional)",
        (
            (
                "ai_classify",
                "Sort the message into your own labels; unclear ones go your way.",
            ),
            (
                "ai_extract",
                "Pick out details like a name, email or date, checked before saving.",
            ),
            ("ai_answer", "Answer from your own facts only; if it can't, hand over."),
        ),
    ),
    ("End", (("stop", "End the automation here."),)),
)

ADVANCED_TYPES = frozenset(
    {"webhook", "set_attribute", "action", "lookup_product", "publish_post"}
)

# Engine trigger types, in the order the trigger picker shows them.
TRIGGER_ORDER = (
    "conversation.message_received",
    "whatsapp.received",
    "contact.created",
    "contact.updated",
    "lead.created",
    "lead.status_changed",
    "lead.qualified",
    "lead.lost",
    "deal.created",
    "deal.stage_changed",
    "order.created",
    "order.paid",
    "conversation.form_completed",
    "manual",
    "business_event",
    "email.opened",
    "email.clicked",
)

# Branch fields an owner can pick from, with the operators that make sense for each.
BRANCH_FIELDS = (
    {"field": "tag", "label": "They have the tag", "ops": ["eq", "ne"], "kind": "text"},
    {
        "field": "within_business_hours",
        "label": "You're open right now",
        "ops": ["eq"],
        "kind": "bool",
    },
    {
        "field": "last_engaged_days",
        "label": "Days since they last replied",
        "ops": ["gte", "lte"],
        "kind": "number",
    },
    {
        "field": "source",
        "label": "Where they came from",
        "ops": ["eq", "ne"],
        "kind": "text",
    },
    {
        "field": "has_order",
        "label": "They've ordered before",
        "ops": ["eq"],
        "kind": "bool",
    },
    {
        "field": "whatsapp_reachable",
        "label": "They can get WhatsApp messages",
        "ops": ["eq"],
        "kind": "bool",
    },
    {
        "field": "first_name",
        "label": "We know their name",
        "ops": ["exists", "not_exists"],
        "kind": "none",
    },
    {
        "field": "email",
        "label": "We have their email",
        "ops": ["exists", "not_exists"],
        "kind": "none",
    },
)


def _reasons(account) -> dict[str, str]:
    """Why a step type can't be used right now, by type (absent = usable)."""
    from apps.core.channels import enabled_channels
    from apps.whatsapp import api as whatsapp_api

    channels = enabled_channels()
    reasons: dict[str, str] = {}
    whatsapp_on = "whatsapp" in channels and (
        whatsapp_api.count_active_business_numbers(account) > 0
    )
    if not whatsapp_on:
        for t in ("send_whatsapp", "send_list"):
            reasons[t] = "Connect WhatsApp first."
    from apps.conversations.models import ConversationForm

    if not ConversationForm.objects.filter(
        account=account, status=ConversationForm.Status.PUBLISHED
    ).exists():
        reasons["start_form"] = "Make a form first (Forms, in the menu)."
    elif not whatsapp_api.template_counts(account).get("approved"):
        reasons["send_whatsapp"] = "You need a message WhatsApp has approved first."
    if "email" not in channels:
        reasons["send_email"] = "Email isn't switched on for your business."
    if not ({"whatsapp", "instagram", "facebook"} & set(channels)):
        for t in ("reply_text", "send_buttons", "wait_for_reply", "ask_question"):
            reasons[t] = "Connect WhatsApp, Instagram or Facebook first."
    return reasons


def step_catalog(account) -> list[dict]:
    """Every step type, grouped, with ``available`` and a ``reason`` when it isn't."""
    from apps.automation.workflow_engine import _STEP_TYPES

    reasons = _reasons(account)
    from apps.ai import api as ai_api

    ai_reason = ai_api.unavailable_reason(account)
    if ai_reason:
        for t in ("ai_classify", "ai_extract", "ai_answer"):
            reasons[t] = f"{ai_reason} (Turn AI on in Settings → AI.)"
    catalog = []
    for group, entries in _STEP_GROUPS:
        for step_type, description in entries:
            if step_type not in _STEP_TYPES:
                continue  # listed ahead of the engine supporting it
            catalog.append(
                {
                    "type": step_type,
                    "group": group,
                    "label": STEP_LABELS.get(step_type, step_type),
                    "description": description,
                    "advanced": step_type in ADVANCED_TYPES,
                    "available": step_type not in reasons,
                    "reason": reasons.get(step_type, ""),
                }
            )
    return catalog


def trigger_catalog() -> list[dict]:
    from apps.automation.workflow_engine import _TRIGGER_TYPES

    return [
        {"type": t, "label": TRIGGER_LABELS.get(t, t)}
        for t in TRIGGER_ORDER
        if t in _TRIGGER_TYPES
    ]

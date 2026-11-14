"""Starter conversations for a new website chatbot (Chatbot Studio P4).

A new bot never starts from an empty page: the owner picks one of these, and it is saved as the
bot's own automation (a ``Workflow`` that starts from website chat), unpublished, so they can
preview it and change the wording before visitors see it. Same pattern as
``apps.automation.workflow_templates``, but only website chat triggers and only steps that run
in website chat.

Every starter falls back to a person: AI answers only from the business's Knowledge, and when it
can't, the chat is handed to the team.
"""

from __future__ import annotations

from typing import Any

_HOUR = 3600

_HANDOVER = {
    "id": "handover",
    "type": "reply_text",
    "text": "Good question. I've passed it to the team, and someone will reply here shortly.",
    "next": "tell_team",
}
_TELL_TEAM = {
    "id": "tell_team",
    "type": "notify_team",
    "to": "owners",
    "text": "{contact} asked something on your website chat that needs a person. "
    "Reply from your inbox.",
    "next": "stop",
}
_STOP = {"id": "stop", "type": "stop"}


STARTERS: dict[str, dict[str, Any]] = {
    "answer-questions": {
        "name": "Answer questions, hand over when stuck",
        "description": "AI answers each question from your Knowledge. When it isn't sure, "
        "it tells the visitor a person will reply and lets your team know.",
        "welcome": "Hi! Ask us anything and we'll answer right here.",
        "definition": {
            "trigger": {"type": "website_chat.message_received"},
            "steps": [
                {
                    "id": "answer",
                    "type": "ai_answer",
                    "next": "stop",
                    "on_handoff": "handover",
                },
                _HANDOVER,
                _TELL_TEAM,
                _STOP,
            ],
        },
    },
    "capture-leads": {
        "name": "Capture sales leads",
        "description": "Greets visitors with two buttons. Anyone who wants a quote is tracked "
        "as interested, asked what they need and how to reach them, and your team is told.",
        "welcome": "",
        "definition": {
            "trigger": {"type": "website_chat.opened"},
            "steps": [
                {
                    "id": "greet",
                    "type": "send_buttons",
                    "text": "Hi! 👋 How can we help today?",
                    "buttons": [{"title": "Get a quote"}, {"title": "Ask a question"}],
                    "next": "choice",
                },
                {
                    "id": "choice",
                    "type": "wait_for_reply",
                    "timeout_seconds": 24 * _HOUR,
                    "routes": {"get-a-quote": "lead", "ask-a-question": "ask"},
                    "on_timeout": "stop",
                },
                {"id": "lead", "type": "create_lead", "next": "need"},
                {
                    "id": "need",
                    "type": "ask_question",
                    "question": "Great! What would you like a quote for?",
                    "attribute": "quote_request",
                    "target": "lead",
                    "max_attempts": 2,
                    "timeout_seconds": 24 * _HOUR,
                    "next": "reach",
                    "on_timeout": "tell_team",
                    "on_invalid": "tell_team",
                    "on_error": "tell_team",
                },
                {
                    "id": "reach",
                    "type": "ask_question",
                    "question": "Thanks. What's the best email or phone number to reach you on?",
                    "attribute": "best_contact",
                    "target": "lead",
                    "max_attempts": 2,
                    "timeout_seconds": 24 * _HOUR,
                    "next": "thanks",
                    "on_timeout": "tell_team",
                    "on_invalid": "tell_team",
                    "on_error": "tell_team",
                },
                {
                    "id": "thanks",
                    "type": "reply_text",
                    "text": "Perfect, thank you! Someone from the team will get back to you soon.",
                    "next": "tell_team",
                },
                {
                    "id": "ask",
                    "type": "reply_text",
                    "text": "Sure, type your question below and we'll answer.",
                    "next": "stop",
                },
                {
                    **_TELL_TEAM,
                    "text": "{contact} asked for a quote on your website chat. "
                    "Their answers are on their lead.",
                },
                _STOP,
            ],
        },
    },
    "find-products": {
        "name": "Help choose a product",
        "description": "Looks up the product a visitor asks about in your catalogue and gives "
        "its price. Anything else is answered by AI from your Knowledge, or handed to your team.",
        "welcome": "Hi! Tell us what you're looking for and we'll check what we have.",
        "definition": {
            "trigger": {"type": "website_chat.message_received"},
            "steps": [
                {
                    "id": "find",
                    "type": "lookup_product",
                    "on_found": "price",
                    "on_ambiguous": "several",
                    "on_no_price": "no_price",
                    "on_not_found": "answer",
                    "on_error": "answer",
                },
                {
                    "id": "price",
                    "type": "reply_text",
                    "text": "{product.name} is {product.currency} {product.price}. "
                    "Would you like to order it, or ask something else?",
                    "next": "stop",
                    "on_error": "answer",
                },
                {
                    "id": "several",
                    "type": "reply_text",
                    "text": "We have a few that could match. Which one do you mean? "
                    "A little more detail helps.",
                    "next": "stop",
                },
                {
                    "id": "no_price",
                    "type": "reply_text",
                    "text": "We have {product.name}. The team will reply here with today's price.",
                    "next": "tell_team",
                    "on_error": "handover",
                },
                {
                    "id": "answer",
                    "type": "ai_answer",
                    "next": "stop",
                    "on_handoff": "handover",
                },
                _HANDOVER,
                _TELL_TEAM,
                _STOP,
            ],
        },
    },
}

DEFAULT_STARTER = "answer-questions"


def list_starters() -> list[dict]:
    return [
        {"key": key, "name": s["name"], "description": s["description"]}
        for key, s in STARTERS.items()
    ]


def install(bot, key: str):
    """Save starter ``key`` as ``bot``'s automation (not yet on) and link it. Returns the Workflow.

    The fields its questions save into are created too, so the draft can be published as is.
    """
    import copy

    from django.db import transaction

    from apps.automation import api as automation_api
    from apps.automation.models import Workflow
    from apps.contacts.models import CustomAttributeDef

    starter = STARTERS.get(key) or STARTERS[DEFAULT_STARTER]
    definition = copy.deepcopy(starter["definition"])
    with transaction.atomic():
        for step in definition["steps"]:
            if step.get("type") == "ask_question" and step.get("attribute"):
                CustomAttributeDef.objects.get_or_create(
                    account=bot.account,
                    entity=step.get("target") or "contact",
                    key=step["attribute"],
                    defaults={"type": "string"},
                )
        name = f"{bot.name} chat"
        workflow = Workflow.objects.create(
            account=bot.account,
            name=name,
            slug=automation_api.unique_workflow_slug(bot.account, name),
            definition=definition,
        )
        bot.workflow = workflow
        bot.save(update_fields=["workflow", "updated_at"])
    return workflow

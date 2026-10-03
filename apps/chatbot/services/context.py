"""
Stateful context builder for chatbot LLM calls.

Every turn passes the full context so the model can answer follow-up questions
("when will it arrive?" after asking about an order) without the visitor
repeating themselves.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from apps.chatbot.models import ChatSession

logger = logging.getLogger(__name__)

_RECENT_MESSAGES = 20


def build(session: ChatSession, query: str) -> dict:
    """Return the context dict passed to the LLM on every turn.

    Keys:
      purpose       — chatbot.purpose
      identity      — contact fields if the session is identified
      messages      — last N messages from the conversation thread
      actions       — enabled ChatbotAction slugs + descriptions
      knowledge     — ranked knowledge excerpts relevant to query
    """
    from apps.chatbot.services.knowledge import retrieve

    return {
        "purpose": session.chatbot.purpose,
        "identity": _identity(session),
        "messages": _recent_messages(session),
        "actions": _enabled_actions(session),
        "knowledge": retrieve(session.chatbot, query),
    }


def _identity(session: ChatSession) -> dict:
    if session.contact_id is None:
        return {
            "identified": False,
            "name": session.visitor_name or None,
            "email": session.visitor_email or None,
        }
    contact = session.contact
    return {
        "identified": True,
        "name": getattr(contact, "full_name", None) or getattr(contact, "name", None),
        "email": getattr(contact, "email", None),
    }


def _recent_messages(session: ChatSession) -> list[dict]:
    if not session.conversation_id:
        return []
    from apps.conversations.models import Message

    msgs = (
        Message.objects.filter(conversation_id=session.conversation_id)
        .order_by("-timestamp")
        .values("direction", "body", "timestamp")[:_RECENT_MESSAGES]
    )
    return [
        {
            "role": "user" if m["direction"] == "inbound" else "assistant",
            "content": m["body"],
        }
        for m in reversed(list(msgs))
    ]


def _enabled_actions(session: ChatSession) -> list[dict]:
    from apps.chatbot.models import ChatbotAction

    return cast(
        "list[dict[str, Any]]",
        list(
            ChatbotAction.objects.filter(
                chatbot=session.chatbot, is_enabled=True
            ).values("slug", "label", "description")
        ),
    )

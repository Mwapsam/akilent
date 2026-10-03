"""
Handoff logic for chatbot sessions.

Two separate steps — by design:
  1. should_handoff() — deterministic check; returns True/False; triggers nothing
  2. perform_handoff() — only called when the widget/visitor confirms

The platform invokes session.chatbot.handoff_action.slug via the action
registry. It never hardcodes "create_support_ticket" or any other slug.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.chatbot.models import ChatSession

logger = logging.getLogger(__name__)

_HANDOFF_PHRASES = frozenset(
    [
        "human",
        "agent",
        "real person",
        "speak to someone",
        "talk to someone",
        "speak to a person",
        "talk to a person",
        "live agent",
        "live support",
    ]
)

_FAILURE_THRESHOLD = 3  # unsatisfied turns before automatic handoff suggestion


def should_handoff(
    session: ChatSession,
    *,
    user_message: str,
    turn_count: int,
    last_reply: str,
    action_failed: bool = False,
    no_knowledge: bool = False,
) -> bool:
    """Return True if the chatbot should suggest a handoff. Does not act."""
    lower = user_message.lower()
    if any(phrase in lower for phrase in _HANDOFF_PHRASES):
        return True
    if action_failed:
        return True
    return turn_count >= _FAILURE_THRESHOLD and no_knowledge


def perform_handoff(session: ChatSession, *, reason: str = "") -> dict:
    """
    Invoke the chatbot's configured handoff action.

    Called only after the visitor explicitly confirms the handoff suggestion.
    Returns {"performed": bool, "ticket_number": str | None}.
    """
    from apps.chatbot.services.actions import invoke

    action = session.chatbot.handoff_action
    if action is None or not action.is_enabled:
        logger.info(
            "chatbot handoff skipped: no enabled handoff_action for chatbot=%s",
            session.chatbot_id,
        )
        return {"performed": False, "ticket_number": None}

    try:
        result = invoke(action.slug, session, reason=reason)
        return {"performed": True, "ticket_number": result.get("ticket_number")}
    except Exception:
        logger.exception("chatbot handoff failed for session=%s", session.session_key)
        return {"performed": False, "ticket_number": None}

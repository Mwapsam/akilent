"""The website chatbot's built-in AI: what answers when no automation does.

``apps.chatbot.services.inbound`` decides *whether* AI answers (a waiting automation or the bot's
own workflow go first). This module produces the answer through ``apps.ai.api.grounded_answer``,
the same answer the inbox's automatic replies use: it may only use the business's own facts
(Knowledge, notes, hours, catalogue) and must pass the same checks. When anything stops it, the
caller hands the chat to the team.

It never takes actions. What the bot *does* (track a lead, assign, notify) is decided by steps the
owner put in the workflow, never by the model.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.chatbot.models import ChatSession

logger = logging.getLogger(__name__)

# A ceiling on AI answers in one chat, below the bot's daily allowance, so a single visitor
# can't use it all up.
_MAX_AI_TURNS_PER_SESSION = 40
_AI_TURNS_TTL = 86_400  # 24 hours, matches session TTL

# A visitor asking for a person gets the team, not another AI answer.
_ASKS_FOR_A_PERSON = re.compile(
    r"\b(human|real person|a person|someone real|agent|live (agent|support|chat)|"
    r"speak to (someone|somebody|a person|a human)|talk to (someone|somebody|a person|a human)|"
    r"customer service|call me)\b",
    re.IGNORECASE,
)


def handle_message(session: ChatSession, user_message: str) -> dict:
    """Process one visitor message (see ``inbound.handle_visitor_message``)."""
    from apps.chatbot.services.inbound import handle_visitor_message

    return handle_visitor_message(session, user_message)


def asks_for_a_person(text: str) -> bool:
    return bool(_ASKS_FOR_A_PERSON.search(text or ""))


def ai_reply(
    session: ChatSession, user_message: str, *, message_id: int | None = None
) -> tuple[str | None, str]:
    """The bot's AI answer to the visitor's latest message: ``(text, "")``, or ``(None, why)``.

    ``why`` is a hand-off reason (``services.handoff.REASONS``) and the chat goes to the team:
    the visitor asked for a person (``asked``); AI is off for the business (provider, plan or
    owner: ``is_available``) or automatic AI replies are switched off site-wide
    (``AI_AUTONOMY_ENABLED``) (``ai_off``); this chat used its AI turns (``turns``); this bot
    used its AI for today (``ai_limit``); or the answer didn't pass the checks (``ai_unsure``).
    """
    from django.conf import settings

    from apps.ai import api as ai_api
    from apps.chatbot.services.ai_budget import take_ai_turn

    account = session.chatbot.account
    why = ""
    if asks_for_a_person(user_message):
        why = "asked"
    elif not ai_api.is_available(account) or not getattr(
        settings, "AI_AUTONOMY_ENABLED", True
    ):
        why = "ai_off"
    elif session.conversation_id is None:
        why = "ai_unsure"
    elif _increment_ai_turns(session.session_key) > _MAX_AI_TURNS_PER_SESSION:
        why = "turns"
    elif not take_ai_turn(session.chatbot_id):
        why = "ai_limit"
    if why:
        logger.info(
            "chatbot AI not answering in session=%s: %s", session.session_key, why
        )
        return None, why

    out = ai_api.grounded_answer(
        session.conversation,
        operation_id=f"chatbot:{session.chatbot_id}:{message_id or session.pk}",
    )
    if not out["ok"]:
        logger.info(
            "chatbot AI declined in session=%s: %s", session.session_key, out["failed"]
        )
        return None, "ai_unsure"
    return out["text"], ""


def _increment_ai_turns(session_key: str) -> int:
    """Atomically increment and return the number of AI turns this session has had."""
    from django.core.cache import cache

    cache_key = f"chatbot_ai_turns:{session_key}"
    if cache.add(cache_key, 1, timeout=_AI_TURNS_TTL):
        return 1
    try:
        return cache.incr(cache_key)
    except ValueError:
        return 1

"""Handing a website chat to the team (Chatbot Studio P5).

Two ways a chat reaches a person:

* **The visitor asks:** the widget's "Talk to a person" button (``/api/chat/handoff/``), or a
  message such as "can I speak to someone". The bot and its AI step back (the conversation's
  automations are paused until a teammate turns them on again), so nothing answers over the
  team, and the visitor is told a person will reply.
* **The bot can't answer:** its AI wasn't sure, is off, or used its daily limit. The visitor
  gets the one-time holding reply (``inbound``); the bot keeps answering what it can.

Either way, the first hand-off in a chat leaves a note in the inbox thread saying why, marks the
conversation unread, and emails the assigned teammate (or the owners). The time and reason are
kept on the session for Analytics. Repeats don't email again.

Replaces the old ``perform_handoff`` / ``handoff_action`` design, which was never wired up: the
team gets the chat in the inbox, which is where they already answer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.chatbot.models import ChatSession

logger = logging.getLogger(__name__)

ASKED = "asked"
REASONS = {
    ASKED: "The visitor asked for a person",
    "ai_unsure": "The bot's AI wasn't sure of the answer",
    "ai_off": "The bot's AI answers are off",
    "ai_limit": "The bot's daily AI limit was reached",
    "turns": "This chat used all its AI answers",
}

HANDOFF_REPLY = (
    "I've asked the team to join. Someone will reply here as soon as they can."
)


def reason_label(reason: str) -> str:
    return REASONS.get(reason, "The bot handed over")


def hand_to_team(session: ChatSession, *, reason: str) -> bool:
    """Hand this chat to the team. True for the chat's first hand-off (the team was told).

    ``reason`` is a key of ``REASONS``. ``ASKED`` also pauses the bot here.
    """
    from django.db import transaction
    from django.utils import timezone

    from apps.chatbot.models import ChatSession as Session
    from apps.chatbot.services.conversation import get_or_create_for_website_chat
    from apps.conversations.models import Message

    reason = reason if reason in REASONS else "ai_unsure"
    conversation = get_or_create_for_website_chat(session)
    now = timezone.now()
    if reason == ASKED and not conversation.automation_paused:
        conversation.pause_automation()  # until a teammate turns automations back on

    first = bool(
        Session.objects.filter(pk=session.pk, handoff_at__isnull=True).update(
            handoff_at=now, handoff_reason=reason
        )
    )
    upgraded = False
    if not first and reason == ASKED and session.handoff_reason != ASKED:
        # The bot handed over earlier; now the visitor asks too. Say so in the thread.
        upgraded = bool(
            Session.objects.filter(pk=session.pk)
            .exclude(handoff_reason=ASKED)
            .update(handoff_reason=ASKED)
        )
    if first:
        session.handoff_at = now
    if first or upgraded:
        session.handoff_reason = reason
        Message.objects.create(
            account=conversation.account,
            conversation=conversation,
            direction=Message.Direction.SYSTEM,
            body=f"Website chat handed to your team: {reason_label(reason)}.",
            timestamp=now,
            metadata={"chatbot_handoff": reason},
        )
        if not conversation.is_unread:
            conversation.is_unread = True
            conversation.save(update_fields=["is_unread", "updated_at"])
    if first:
        from apps.chatbot.tasks import notify_handoff

        session_id = session.pk
        transaction.on_commit(lambda: notify_handoff.delay(session_id))
    return first


def tell_the_team(session: ChatSession) -> int:
    """Email the assigned teammate (else the owners) that a website visitor needs a person."""
    from django.urls import reverse

    from apps.accounts import notifications

    conversation = session.conversation
    if conversation is None:
        return 0
    contact = conversation.contact
    who = (
        getattr(contact, "full_name", "")
        or session.visitor_name
        or session.visitor_email
        or "A website visitor"
    )
    reason = reason_label(session.handoff_reason)
    try:
        return notifications.notify_team(
            conversation.account,
            to="assignee",
            subject=f"Website chat: {who} needs a person"[:200],
            text=f"{who} is waiting on your website chat ({session.chatbot.name}). "
            f"{reason}. Reply from your inbox and they'll see it in the chat.",
            path=reverse("conversations:detail", args=[conversation.public_id]),
            conversation=conversation,
            reason="You're getting this because a visitor on your website chat needs a "
            "person.",
        )
    except notifications.NotifyError:
        logger.exception("chatbot handoff: could not notify for session=%s", session.pk)
        return 0

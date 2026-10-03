"""
Conversation service for website chat sessions.

Mirrors the WhatsApp pipeline but for the WEBSITE_CHAT channel.
Each ChatSession gets at most one Conversation (OneToOne); this service
creates it lazily on the first message.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from apps.conversations.models import Conversation

logger = logging.getLogger(__name__)


def get_or_create_for_website_chat(session: object) -> Conversation:
    """Return the Conversation for this session, creating it if needed.

    Uses select_for_update inside a transaction to prevent two concurrent first
    messages from each creating a separate Conversation for the same session.
    """
    from django.db import transaction

    from apps.chatbot.models import ChatSession
    from apps.conversations.models import Conversation

    assert isinstance(session, ChatSession)

    # Fast path: conversation already set (no lock needed).
    if session.conversation_id:
        return session.conversation  # type: ignore[return-value]

    contact = session.contact
    if contact is None:
        raise ValueError("Cannot create a conversation for an unidentified session.")

    with transaction.atomic():
        # Re-read the session under a row lock so concurrent calls serialise here.
        locked = ChatSession.objects.select_for_update().get(pk=session.pk)
        if locked.conversation_id:
            # Another concurrent request already created it.
            session.conversation_id = locked.conversation_id
            assert locked.conversation is not None
            return locked.conversation

        conversation = Conversation.objects.create(
            account=session.chatbot.account,
            contact=contact,
            channel=Conversation.Channel.WEBSITE_CHAT,
        )
        locked.conversation = conversation
        locked.save(update_fields=["conversation", "last_activity_at"])

    session.conversation = conversation
    session.conversation_id = conversation.pk
    return conversation


def record_inbound_chatbot_message(session: object, body: str) -> object:
    """Persist an inbound message from the visitor into the conversation thread."""
    from apps.chatbot.models import ChatSession
    from apps.conversations.models import Message

    assert isinstance(session, ChatSession)

    conversation = get_or_create_for_website_chat(session)

    message = Message.objects.create(
        account=session.chatbot.account,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        body=body,
        timestamp=timezone.now(),
    )

    session.last_activity_at = timezone.now()
    session.save(update_fields=["last_activity_at"])

    return message


def record_outbound_chatbot_message(session: object, body: str) -> object:
    """Persist the bot's reply into the conversation thread."""
    from apps.chatbot.models import ChatSession
    from apps.conversations.models import Message

    assert isinstance(session, ChatSession)

    if not session.conversation_id:
        raise ValueError("No conversation exists for this session yet.")

    assert session.conversation is not None
    message = Message.objects.create(
        account=session.chatbot.account,
        conversation=session.conversation,
        direction=Message.Direction.OUTBOUND,
        body=body,
        timestamp=timezone.now(),
        metadata={"source": "chatbot"},
    )

    session.last_activity_at = timezone.now()
    session.save(update_fields=["last_activity_at"])

    return message

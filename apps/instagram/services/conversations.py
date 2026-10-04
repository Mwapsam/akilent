from __future__ import annotations

import logging

from apps.instagram.models.contact import InstagramContact
from apps.instagram.models.conversation import InstagramConversation

logger = logging.getLogger(__name__)


def get_or_create_instagram_conversation(
    ig_contact: InstagramContact,
) -> tuple[InstagramConversation, "conversations.Conversation"]:
    """
    Idempotently get/create an InstagramConversation and its spine wrapper.

    Returns (instagram_conversation, spine_conversation).
    """
    from apps.conversations.models import Conversation

    ig_convo = InstagramConversation.get_or_open(ig_contact)
    spine = Conversation.get_or_create_for_instagram(ig_convo)
    return ig_convo, spine

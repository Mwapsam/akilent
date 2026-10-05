from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apps.instagram.models.contact import InstagramContact
from apps.instagram.models.conversation import InstagramConversation

if TYPE_CHECKING:
    from apps.conversations.models import Conversation

logger = logging.getLogger(__name__)


def get_or_create_instagram_conversation(
    ig_contact: InstagramContact,
) -> tuple[InstagramConversation, Conversation]:
    """
    Idempotently get/create an InstagramConversation and its spine wrapper.

    Returns (instagram_conversation, spine_conversation).
    """
    from apps.conversations.models import Conversation

    ig_convo = InstagramConversation.get_or_open(ig_contact)
    spine = Conversation.get_or_create_for_channel(
        ig_convo, Conversation.Channel.INSTAGRAM
    )
    return ig_convo, spine

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apps.instagram.models.contact import InstagramContact
from apps.instagram.models.conversation import InstagramConversation

if TYPE_CHECKING:
    from apps.conversations.models import Conversation

logger = logging.getLogger(__name__)


def get_or_create_instagram_conversation(
    ig_contact: InstagramContact, instagram_account=None
) -> tuple[InstagramConversation, Conversation]:
    """
    Idempotently get/create an InstagramConversation and its spine wrapper.

    Returns (instagram_conversation, spine_conversation). A spine Conversation
    created here is routed to a team/agent, exactly once — the same rule the
    WhatsApp path follows.
    """
    from apps.conversations.models import ChannelConversation, Conversation
    from apps.conversations.services import route_new_conversation

    ig_convo = InstagramConversation.get_or_open(ig_contact, instagram_account)
    is_new = not ChannelConversation.objects.filter(
        channel=Conversation.Channel.INSTAGRAM, object_id=ig_convo.pk
    ).exists()
    spine = Conversation.get_or_create_for_channel(
        ig_convo, Conversation.Channel.INSTAGRAM
    )
    if is_new:
        route_new_conversation(spine)
    return ig_convo, spine

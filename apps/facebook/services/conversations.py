from __future__ import annotations

from typing import TYPE_CHECKING

from apps.facebook.models.contact import FacebookContact
from apps.facebook.models.conversation import FacebookConversation

if TYPE_CHECKING:
    from apps.conversations.models import Conversation


def get_or_create_facebook_conversation(
    fb_contact: FacebookContact, page_id: str
) -> tuple[FacebookConversation, Conversation]:
    """
    Idempotently get/create a FacebookConversation and its spine wrapper.

    Returns (facebook_conversation, spine_conversation). A spine Conversation created
    here is routed to a team/agent exactly once, as on WhatsApp and Instagram.
    """
    from apps.conversations.models import ChannelConversation, Conversation
    from apps.conversations.services import route_new_conversation

    fb_convo = FacebookConversation.get_or_open(fb_contact, page_id)
    is_new = not ChannelConversation.objects.filter(
        channel=Conversation.Channel.FACEBOOK, object_id=fb_convo.pk
    ).exists()
    spine = Conversation.get_or_create_for_channel(
        fb_convo, Conversation.Channel.FACEBOOK
    )
    if is_new:
        route_new_conversation(spine)
    return fb_convo, spine

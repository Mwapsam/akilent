"""Record which published post started a conversation.

Only hard evidence counts: the customer's message carries a tracked link's ref code,
or a private reply to a comment on the post opened the DM. A conversation with neither
stays unattributed; nothing here guesses from timing or "the latest post".

Inbound hooks never raise: a failure here must not stop a message reaching the inbox.
"""

from __future__ import annotations

import logging

from apps.conversations.models import ConversationEntryPoint

logger = logging.getLogger(__name__)


def on_inbound_message(conversation, message) -> None:
    """Called for every new inbound message, on WhatsApp and Instagram."""
    try:
        match_ref_code(conversation, message)
    except Exception:
        logger.exception("Ref-code match failed for message=%s", message.pk)
    try:
        record_dm_reply(conversation, message)
    except Exception:
        logger.exception("DM-reply signal failed for message=%s", message.pk)


def match_ref_code(conversation, message) -> ConversationEntryPoint | None:
    """Link the conversation to the post whose tracked-link code the message carries.

    The code must belong to a link of the same workspace; another business's code is
    ignored.
    """
    from apps.publishing.links import find_code
    from apps.publishing.models import TrackedLink

    code = find_code(message.body)
    if not code:
        return None
    link = (
        TrackedLink.objects.filter(account_id=conversation.account_id, code=code)
        .select_related("post", "variant")
        .first()
    )
    if link is None:
        return None
    entry, _ = ConversationEntryPoint.objects.get_or_create(
        conversation=conversation,
        post=link.post,
        kind=ConversationEntryPoint.Kind.TRACKED_LINK,
        defaults={
            "account_id": conversation.account_id,
            "link": link,
            "variant": link.variant,
            "campaign": link.campaign,
            "message": message,
        },
    )
    return entry


def record_comment_entry(thread) -> ConversationEntryPoint | None:
    """A private reply to a comment on a Content Studio post opened this DM."""
    if not (thread.content_variant_id and thread.conversation_id):
        return None
    variant = thread.content_variant
    post = variant.post
    entry, _ = ConversationEntryPoint.objects.get_or_create(
        conversation_id=thread.conversation_id,
        post=post,
        kind=ConversationEntryPoint.Kind.IG_COMMENT,
        defaults={
            "account_id": post.account_id,
            "variant": variant,
            "campaign": post.plan_item.campaign if post.plan_item else None,
        },
    )
    return entry


def first_entry_point(conversation) -> ConversationEntryPoint | None:
    """The earliest explicit touch, which gets the credit."""
    if conversation is None:
        return None
    return (
        ConversationEntryPoint.objects.filter(conversation=conversation)
        .select_related("post", "variant", "campaign")
        .first()
    )


def record_dm_reply(conversation, message) -> None:
    """The customer answered a DM sent from an insight: record dm_replied_at once."""
    from apps.insights.actions import record_outcome_signal
    from apps.insights.models import RecommendationLog

    rec = (
        RecommendationLog.objects.filter(
            conversation=conversation,
            acted_at__isnull=False,
            acted_at__lte=message.timestamp,
        )
        .order_by("-acted_at")
        .first()
    )
    if rec is None:
        return
    signals = rec.outcome_summary or {}
    if signals.get("dm_sent_at") and not signals.get("dm_replied_at"):
        record_outcome_signal(rec, "dm_replied_at")

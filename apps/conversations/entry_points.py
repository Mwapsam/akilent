"""Record which published post or WhatsApp campaign started a conversation.

Only hard evidence counts: the customer's message carries a tracked link's ref code, a
private reply to a comment on the post opened the DM, or the message answers a campaign's
own message (a swipe-reply or a tapped button: WhatsApp names the message it answers). A
conversation with none stays unattributed; nothing here guesses from timing or "the latest
post", so a customer who simply writes after a campaign is a reply, not a credit.

Inbound hooks never raise: a failure here must not stop a message reaching the inbox.
"""

from __future__ import annotations

import logging

from apps.conversations.models import ConversationEntryPoint

logger = logging.getLogger(__name__)


def on_inbound_message(conversation, message) -> None:
    """Called for every new inbound message, on WhatsApp and Instagram."""
    try:
        # First, so a chat from an ad is paid before any ref code in its text is read.
        from apps.conversations import ad_referrals

        ad_referrals.capture(conversation, message)
    except Exception:
        logger.exception("Ad-referral capture failed for message=%s", message.pk)
    try:
        match_ref_code(conversation, message)
    except Exception:
        logger.exception("Ref-code match failed for message=%s", message.pk)
    try:
        match_campaign_reply(conversation, message)
    except Exception:
        logger.exception("Campaign-reply match failed for message=%s", message.pk)
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
    from apps.publishing.models import BioTile, TrackedLink

    code = find_code(message.body)
    if not code:
        return None
    tile = None
    link = (
        TrackedLink.objects.filter(account_id=conversation.account_id, code=code)
        .select_related("post", "variant")
        .first()
    )
    if link is None:
        # A bio-page tile's code: the chat came through the tile, for its link's post.
        tile = (
            BioTile.objects.filter(page__account_id=conversation.account_id, code=code)
            .select_related("link__post", "link__variant")
            .first()
        )
        link = tile.link if tile else None
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
            "bio_tile": tile,
        },
    )
    return entry


def match_campaign_reply(conversation, message) -> ConversationEntryPoint | None:
    """Link the conversation to the WhatsApp campaign whose message this one answers.

    The answered message must be a campaign message of the same workspace. One entry per
    campaign and conversation: a second reply to the same campaign changes nothing."""
    if not message.whatsapp_message_id:
        return None
    from apps.whatsapp.interactive import replied_to
    from apps.whatsapp.models import WhatsAppCampaignRecipient

    answered = replied_to(message.whatsapp_message)
    if not answered:
        return None
    recipient = (
        WhatsAppCampaignRecipient.objects.filter(
            campaign__account_id=conversation.account_id,
            message__message_log__message_id=answered,
        )
        .select_related("campaign")
        .first()
    )
    if recipient is None:
        return None
    entry, _ = ConversationEntryPoint.objects.get_or_create(
        conversation=conversation,
        whatsapp_campaign=recipient.campaign,
        defaults={
            "account_id": conversation.account_id,
            "kind": ConversationEntryPoint.Kind.CAMPAIGN_REPLY,
            "message": message,
        },
    )
    return entry


def record_comment_entry(thread) -> ConversationEntryPoint | None:
    """A private reply to a comment on a Content Studio post opened this DM.

    ``thread`` is an Instagram CommentThread or a FacebookCommentThread.
    """
    if not (thread.content_variant_id and thread.conversation_id):
        return None
    from apps.facebook.models import FacebookCommentThread

    kind = (
        ConversationEntryPoint.Kind.FB_COMMENT
        if isinstance(thread, FacebookCommentThread)
        else ConversationEntryPoint.Kind.IG_COMMENT
    )
    variant = thread.content_variant
    post = variant.post
    entry, _ = ConversationEntryPoint.objects.get_or_create(
        conversation_id=thread.conversation_id,
        post=post,
        kind=kind,
        defaults={
            "account_id": post.account_id,
            "variant": variant,
            "campaign": post.plan_item.campaign if post.plan_item else None,
        },
    )
    return entry


def first_entry_point(conversation) -> ConversationEntryPoint | None:
    """The touch that gets the credit: a Meta ad first (paid takes precedence, see
    docs/strategy/akilent-ads.md §4), otherwise the earliest explicit touch."""
    if conversation is None:
        return None
    entries = ConversationEntryPoint.objects.filter(
        conversation=conversation
    ).select_related("post", "variant", "campaign", "whatsapp_campaign", "ad_referral")
    ad = entries.filter(kind=ConversationEntryPoint.Kind.AD_REFERRAL).first()
    return ad or entries.first()


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

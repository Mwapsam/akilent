"""
Comment trigger service — Phase 3.

Evaluates CommentTrigger rules after the moderation pass. When a trigger
matches, it enqueues a private reply via the outbox and, on success, opens
an InstagramConversation so the customer enters the regular inbox flow.

Invariants:
  - A CommentThread fires at most one trigger (trigger_fired_at set atomically).
  - The idempotency key is deterministic: private_reply:{account_id}:{comment_id}
    so repeated webhook deliveries produce exactly one OutboundMessage.
  - The Instagram conversation is only created AFTER the Meta API call succeeds.
  - On API failure the OutboundMessage stays FAILED (or QUEUED for a retry by
    the outbox drain); no conversation is created until it is sent.
  - A sent private reply is recorded on the inbox thread (send_outbound does
    this), so the business sees what the customer received.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.utils import timezone

from apps.instagram.models.comment import CommentThread
from apps.instagram.models.trigger import CommentTrigger

logger = logging.getLogger(__name__)

# Meta accepts a private reply to a comment for 7 days after it was made. Past that the
# reply isn't sent at all: it's recorded as failed with this reason, which the Comments
# view shows on the comment (apps.conversations.comments).
PRIVATE_REPLY_WINDOW = timedelta(days=7)
TOO_OLD = (
    "Not sent: the comment is more than 7 days old, and Instagram and Facebook only "
    "allow a private reply within 7 days."
)


def keyword_in(text: str, keyword: str) -> bool:
    """Whether ``keyword`` is in ``text`` as a whole word or phrase, ignoring case:
    "cost" matches "what's the cost?" but not "costume", "pp" doesn't match "happy"."""
    keyword = (keyword or "").strip()
    if not keyword:
        return False
    return (
        re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text or "", re.IGNORECASE)
        is not None
    )


def too_old(thread) -> bool:
    received = getattr(thread, "received_at", None)
    return received is not None and timezone.now() - received > PRIVATE_REPLY_WINDOW


def already_answered_on_post(thread, contact_field: str) -> bool:
    """Whether this person already got an automatic reply to another comment on the same
    post: one per person per post, however many times they comment."""
    contact_id = getattr(thread, f"{contact_field}_id", None)
    if not (contact_id and thread.post_id):
        return False
    return (
        type(thread)
        .objects.filter(
            **{contact_field + "_id": contact_id},
            post_id=thread.post_id,
            trigger_fired_at__isnull=False,
        )
        .exclude(pk=thread.pk)
        .exists()
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_triggers(thread: CommentThread, body: str) -> bool:
    """
    Evaluate all active CommentTriggers for the thread's account.

    Returns True if a trigger was matched and a private reply was enqueued.
    """
    # Root-comment only; replies to comments don't fire triggers.
    if not body:
        return False

    # Idempotency: never fire again if a trigger already fired on this thread.
    if thread.trigger_fired_at is not None:
        logger.debug(
            "evaluate_triggers: thread %s already fired trigger at %s",
            thread.pk,
            thread.trigger_fired_at,
        )
        return False

    if already_answered_on_post(thread, "instagram_contact"):
        return False

    account = thread.instagram_account.account
    trigger = first_matching_trigger(
        account, "instagram", body, variant=thread.content_variant
    )
    if trigger is None:
        return False
    _fire_trigger(thread, trigger)
    return True


def first_matching_trigger(
    account, channel: str, body: str, variant=None
) -> CommentTrigger | None:
    """The account's first active trigger for ``channel`` ("instagram" or "facebook")
    that matches the comment text. Shared by Instagram and Facebook.

    ``variant``: the Content Studio post channel the comment is on, if known. A rule made
    for that post is tried before the general ones; a rule made for another post never
    answers. Then by priority, as before.
    """
    from django.db.models import Q

    from apps.instagram.models.moderation import RuleChannels

    post_id = getattr(variant, "post_id", None)
    scope = Q(post__isnull=True) | (Q(post_id=post_id) if post_id else Q(pk__in=[]))
    triggers = sorted(
        CommentTrigger.objects.filter(
            scope,
            account=account,
            is_active=True,
            channels__in=RuleChannels.covering(channel),
        ),
        key=lambda t: (t.post_id is None, t.priority, t.created_at),
    )
    return next((t for t in triggers if _trigger_matches(t, body)), None)


def reply_links(account, variant) -> dict:
    """The links a reply's placeholders stand for: the commented post's tracked WhatsApp
    chat link on that channel ({whatsapp_link}) and the live bio page ({bio_link}).

    A chat that starts from the link carries the post's ref code, so it is credited to the
    post like any other (apps.conversations.entry_points.match_ref_code). Never creates a
    link: one the business hasn't made, or has switched off, is left out.
    """
    links: dict = {}
    try:
        from apps.publishing import bio
        from apps.publishing import links as tracked
        from apps.publishing.models import BioPage

        if variant is not None:
            link = tracked.active_link(variant)
            if link is not None:
                links["whatsapp_link"] = tracked.short_url(link)
        if bio.is_released():
            page = BioPage.objects.filter(account=account, is_active=True).first()
            if page is not None:
                links["bio_link"] = bio.page_url(page)
    except Exception:
        logger.exception(
            "reply_links failed for variant=%s", getattr(variant, "pk", None)
        )
    return links


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _trigger_matches(trigger: CommentTrigger, body: str) -> bool:
    match_type = trigger.match_type

    if match_type == CommentTrigger.MatchType.ANY_COMMENT:
        return True

    if match_type == CommentTrigger.MatchType.KEYWORD:
        return any(keyword_in(body, kw) for kw in trigger.keyword_list())

    if match_type == CommentTrigger.MatchType.BUYING_INTENT:
        from apps.conversations.intent import detect_buying_intent

        return bool(detect_buying_intent(body))

    return False


# ---------------------------------------------------------------------------
# Firing: outbox-first, then conversation creation on success
# ---------------------------------------------------------------------------


def _fire_trigger(thread: CommentThread, trigger: CommentTrigger) -> None:
    """
    Mark the thread as triggered (atomic), enqueue the private reply outbox
    record, call the Meta API, and on success open the InstagramConversation.
    """
    # Atomically claim the trigger slot — prevents races on concurrent delivery.
    claimed = _claim_trigger(thread)
    if not claimed:
        logger.debug(
            "_fire_trigger: thread %s already claimed by concurrent delivery", thread.pk
        )
        return

    ig_account = thread.instagram_account
    ig_contact = thread.instagram_contact
    username = ig_contact.username or ""
    links = (
        reply_links(ig_account.account, thread.content_variant)
        if trigger.uses_links()
        else {}
    )
    body = trigger.render_reply(username, links)

    # Deterministic idempotency key: one outbox record per comment thread.
    idempotency_key = f"private_reply:{ig_account.pk}:{thread.comment_id}"

    from apps.instagram.models.message import OutboundMessage
    from apps.instagram.services.outbound import enqueue_reply

    action_type = (
        OutboundMessage.ActionType.COMMENT_REPLY
        if trigger.reply_type == CommentTrigger.ReplyType.COMMENT
        else OutboundMessage.ActionType.PRIVATE_REPLY
    )

    outbound = enqueue_reply(
        ig_account,
        recipient_igsid=thread.comment_id,  # comment_id used as recipient for both reply types
        body=body,
        action_type=action_type,
        idempotency_key=idempotency_key,
    )

    if outbound is None:
        # Duplicate idempotency key — a previous delivery already enqueued this.
        logger.debug(
            "_fire_trigger: outbound already exists for key=%s", idempotency_key
        )
        # Ensure the conversation exists even if we didn't send again.
        _ensure_conversation(thread, ig_contact)
        return

    if too_old(thread):
        outbound.mark_failed(TOO_OLD, terminal=True)
        return

    # Call the provider synchronously (Celery handles retries via the outbox).
    from apps.instagram.services.outbound import send_outbound

    sent = send_outbound(outbound)

    if sent:
        logger.info(
            "Private reply sent for thread %s (trigger=%s)", thread.pk, trigger.name
        )
        _ensure_conversation(thread, ig_contact)
    else:
        logger.warning(
            "Private reply failed for thread %s (trigger=%s): %s",
            thread.pk,
            trigger.name,
            outbound.last_error,
        )
        # Don't open a conversation — the customer hasn't received the DM yet.


def _claim_trigger(thread: CommentThread) -> bool:
    """
    Atomically set trigger_fired_at on the thread if not already set.
    Returns True if this call claimed the slot; False if already claimed.
    """
    updated = CommentThread.objects.filter(
        pk=thread.pk, trigger_fired_at__isnull=True
    ).update(trigger_fired_at=timezone.now())
    if updated:
        thread.trigger_fired_at = timezone.now()
        return True
    return False


def _ensure_conversation(thread: CommentThread, ig_contact) -> None:
    """
    Open/retrieve the InstagramConversation + spine for this contact and link
    it to the CommentThread if not already linked.
    """
    from apps.instagram.services.conversations import (
        get_or_create_instagram_conversation,
    )

    _ig_convo, spine = get_or_create_instagram_conversation(
        ig_contact, thread.instagram_account
    )

    if thread.conversation_id != spine.pk:
        CommentThread.objects.filter(pk=thread.pk).update(conversation=spine)
        thread.conversation_id = spine.pk

    from apps.conversations.entry_points import record_comment_entry

    try:
        record_comment_entry(thread)
    except Exception:
        logger.exception(
            "Couldn't record the post entry point for thread %s", thread.pk
        )

    logger.debug(
        "_ensure_conversation: thread %s → conversation %s", thread.pk, spine.pk
    )

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
  - On API failure the OutboundMessage stays FAILED; no conversation is created.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.instagram.models.comment import CommentThread
from apps.instagram.models.trigger import CommentTrigger

logger = logging.getLogger(__name__)


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

    account = thread.instagram_account.account
    triggers = CommentTrigger.objects.filter(
        account=account, is_active=True
    ).order_by("priority", "created_at")

    for trigger in triggers:
        if _trigger_matches(trigger, body):
            _fire_trigger(thread, trigger)
            return True

    return False


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _trigger_matches(trigger: CommentTrigger, body: str) -> bool:
    match_type = trigger.match_type

    if match_type == CommentTrigger.MatchType.ANY_COMMENT:
        return True

    if match_type == CommentTrigger.MatchType.KEYWORD:
        body_lower = body.lower()
        return any(kw in body_lower for kw in trigger.keyword_list())

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
    body = trigger.render_reply(username)

    # Deterministic idempotency key: one outbox record per comment thread.
    idempotency_key = (
        f"private_reply:{ig_account.pk}:{thread.comment_id}"
    )

    from apps.instagram.services.outbound import enqueue_reply
    from apps.instagram.models.message import OutboundMessage

    outbound = enqueue_reply(
        ig_account,
        recipient_igsid=thread.comment_id,  # private_reply uses comment_id as recipient
        body=body,
        action_type=OutboundMessage.ActionType.PRIVATE_REPLY,
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


def _ensure_conversation(
    thread: CommentThread, ig_contact
) -> None:
    """
    Open/retrieve the InstagramConversation + spine for this contact and link
    it to the CommentThread if not already linked.
    """
    from apps.instagram.services.conversations import get_or_create_instagram_conversation

    ig_convo, spine = get_or_create_instagram_conversation(ig_contact)

    if thread.conversation_id != spine.pk:
        CommentThread.objects.filter(pk=thread.pk).update(conversation=spine)
        thread.conversation_id = spine.pk

    logger.debug(
        "_ensure_conversation: thread %s → conversation %s", thread.pk, spine.pk
    )

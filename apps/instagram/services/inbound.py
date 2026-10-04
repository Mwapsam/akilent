from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone

from apps.instagram.models.account import InstagramBusinessAccount
from apps.instagram.models.comment import Comment, CommentThread
from apps.instagram.models.message import InstagramMessage
from apps.instagram.models.webhook import WebhookEventLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DM handling
# ---------------------------------------------------------------------------


def handle_dm(event: WebhookEventLog) -> None:
    """
    Process an inbound Instagram DM webhook event.

    Order: persist contact → persist conversation → persist message → fire
    MessageReceived → run intent detection.
    """
    payload = event.raw_payload
    instagram_account = event.instagram_account

    try:
        messaging = _extract_dm_messaging(payload)
        if not messaging:
            logger.debug("handle_dm: no messaging entry in event %s", event.pk)
            return

        for entry in messaging:
            _process_dm_entry(instagram_account, entry)
    except Exception:
        logger.exception("handle_dm failed for event %s", event.pk)
        raise


def _process_dm_entry(
    instagram_account: InstagramBusinessAccount, entry: dict
) -> None:
    from apps.instagram.services.contacts import resolve_or_create_contact
    from apps.instagram.services.conversations import get_or_create_instagram_conversation
    from apps.instagram.services.intent import evaluate_intent
    from apps.core.events import MessageReceived, dispatcher

    sender = entry.get("sender", {})
    igsid = sender.get("id", "")
    if not igsid:
        return

    message = entry.get("message", {})
    message_id = message.get("mid", "")
    body = message.get("text", "")
    ts_ms = entry.get("timestamp", 0)
    timestamp = datetime.fromtimestamp(ts_ms / 1000, tz=dt_timezone.utc) if ts_ms else timezone.now()

    # 1. Resolve channel identity → canonical Contact
    ig_contact = resolve_or_create_contact(instagram_account, igsid)

    # 2. Resolve/create DM conversation + spine
    ig_convo, spine = get_or_create_instagram_conversation(ig_contact)
    ig_convo.register_inbound(timestamp)

    # 3. Persist the message (idempotent on message_id)
    if message_id and InstagramMessage.objects.filter(message_id=message_id).exists():
        logger.debug("Duplicate Instagram message %s — skipping", message_id)
        return

    ig_message = InstagramMessage.objects.create(
        conversation=ig_convo,
        message_id=message_id or _synthetic_message_id(igsid, ts_ms),
        direction=InstagramMessage.Direction.INBOUND,
        body=body,
        timestamp=timestamp,
        metadata=entry,
    )

    # 4. Update spine
    spine.register_inbound(timestamp)

    # 5. Fire domain event (consumed by AI, automation, analytics)
    dispatcher.publish(
        MessageReceived(
            account_id=instagram_account.account_id,
            contact_id=ig_contact.contact_id,
            message_id=ig_message.message_id,
            channel="instagram",
            body=body,
            message_type="text",
            occurred_at=timestamp,
        )
    )

    # 6. Intent detection → AIProposal (never auto-creates a Lead)
    if body and ig_contact.contact_id:
        evaluate_intent(
            instagram_account.account,
            spine,
            body,
            source_description="DM",
        )


# ---------------------------------------------------------------------------
# Comment handling
# ---------------------------------------------------------------------------


def handle_comment(event: WebhookEventLog) -> None:
    """
    Process an inbound Instagram comment webhook event.

    Comments are stored as CommentThread + Comment records. A Conversation is
    NOT created here — only a private reply (triggered separately) creates one.
    """
    payload = event.raw_payload
    instagram_account = event.instagram_account

    try:
        comment_entries = _extract_comment_entries(payload)
        for entry in comment_entries:
            _process_comment_entry(instagram_account, entry)
    except Exception:
        logger.exception("handle_comment failed for event %s", event.pk)
        raise


def _process_comment_entry(
    instagram_account: InstagramBusinessAccount, entry: dict
) -> None:
    from apps.instagram.services.contacts import resolve_or_create_contact
    from apps.conversations.intent import detect_buying_intent

    commenter = entry.get("from", {})
    igsid = commenter.get("id", "")
    if not igsid:
        return

    comment_id = entry.get("id", "")
    body = entry.get("text", "")
    parent_id = entry.get("parent_id", "")
    post_id = entry.get("media", {}).get("id", "")
    ts_str = entry.get("timestamp", "")

    try:
        timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        timestamp = timezone.now()

    ig_contact = resolve_or_create_contact(
        instagram_account,
        igsid,
        username=commenter.get("username", ""),
        name=commenter.get("name", ""),
    )

    # Upsert the CommentThread (one thread per root comment_id)
    root_id = parent_id or comment_id
    thread, _ = CommentThread.objects.get_or_create(
        instagram_account=instagram_account,
        comment_id=root_id,
        defaults={
            "post_id": post_id,
            "instagram_contact": ig_contact,
            "body": body if not parent_id else "",
            "received_at": timestamp,
        },
    )

    # Persist the individual Comment (idempotent on thread + comment_id)
    Comment.objects.get_or_create(
        thread=thread,
        comment_id=comment_id,
        defaults={
            "instagram_contact": ig_contact,
            "parent_comment_id": parent_id,
            "body": body,
            "direction": Comment.Direction.INBOUND,
            "timestamp": timestamp,
        },
    )

    # Detect buying intent on root comments only
    if not parent_id and body:
        intent_phrase = detect_buying_intent(body)
        if intent_phrase and not thread.intent:
            thread.intent = intent_phrase
            thread.save(update_fields=["intent"])
            logger.info(
                "Buying intent detected in comment %s: %r", comment_id, intent_phrase
            )
            # A DM-first private reply would be triggered here in Phase 3
            # (CommentTrigger evaluation). For Phase 1, we log and stop.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_dm_messaging(payload: dict) -> list[dict]:
    """Extract messaging entries from an Instagram DM webhook payload."""
    messaging = []
    for entry in payload.get("entry", []):
        messaging.extend(entry.get("messaging", []))
    return messaging


def _extract_comment_entries(payload: dict) -> list[dict]:
    """Extract comment entries from an Instagram comment webhook payload."""
    entries = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            if change.get("field") == "comments":
                entries.append(value)
    return entries


def _synthetic_message_id(igsid: str, ts_ms: int) -> str:
    """Fallback message ID when Meta doesn't provide one."""
    return f"synthetic_{igsid}_{ts_ms}"

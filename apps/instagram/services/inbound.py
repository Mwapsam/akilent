from __future__ import annotations

import logging
from datetime import UTC, datetime

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
    if instagram_account is None:
        logger.error("handle_dm: event %s has no instagram_account", event.pk)
        return

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


def _process_dm_entry(instagram_account: InstagramBusinessAccount, entry: dict) -> None:
    from apps.instagram.services.contacts import resolve_or_create_contact
    from apps.instagram.services.conversations import (
        get_or_create_instagram_conversation,
    )
    from apps.instagram.services.intent import evaluate_intent

    sender = entry.get("sender", {})
    igsid = sender.get("id", "")
    if not igsid:
        return

    message = entry.get("message", {})
    message_id = message.get("mid", "")
    body = message.get("text", "") or _extract_message_body(message)
    ts_ms = entry.get("timestamp", 0)
    timestamp = (
        datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else timezone.now()
    )

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

    # 4. Project onto the canonical spine (Message + Event + workflow enrollment)
    from apps.conversations.services import record_inbound_instagram_message

    record_inbound_instagram_message(
        contact=ig_contact.contact,
        instagram_conversation=ig_convo,
        instagram_message=ig_message,
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
    if instagram_account is None:
        logger.error("handle_comment: event %s has no instagram_account", event.pk)
        return

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
    from apps.conversations.intent import detect_buying_intent
    from apps.instagram.services.contacts import resolve_or_create_contact

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

    # Run moderation rules (Phase 2) — moderation acts on Instagram content;
    # automation triggers act on Akilent state. Both are evaluated here.
    comment_obj = Comment.objects.filter(thread=thread, comment_id=comment_id).first()
    if comment_obj:
        from apps.instagram.services.moderation import moderate_comment

        moderate_comment(comment_obj)

    # Evaluate CommentTriggers (Phase 3) — root comments only; triggers open a DM.
    if not parent_id and body:
        from apps.instagram.services.triggers import evaluate_triggers

        evaluate_triggers(thread, body)

    # Detect buying intent on root comments only
    if not parent_id and body:
        intent_phrase = detect_buying_intent(body)
        if intent_phrase and not thread.intent:
            thread.intent = intent_phrase
            thread.save(update_fields=["intent"])
            logger.info(
                "Buying intent detected in comment %s: %r", comment_id, intent_phrase
            )


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
    """Extract comment and mention entries from an Instagram webhook payload."""
    entries = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            field = change.get("field", "")
            if field in {"comments", "mentions"}:
                value = change.get("value", {})
                # Normalise mention payloads to the same shape as comment entries.
                # Mentions arrive with media.id being the media where the account
                # was tagged. They're treated as comments for storage purposes.
                if field == "mentions":
                    value = _normalise_mention(value)
                entries.append(value)
    return entries


def _normalise_mention(value: dict) -> dict:
    """Reshape a mention change.value to look like a comment entry."""
    return {
        "id": value.get("comment_id", ""),
        "text": value.get("text", ""),
        "from": value.get("from", {}),
        "media": {"id": value.get("media_id", "")},
        "timestamp": value.get("timestamp", ""),
        "parent_id": value.get("parent_id", ""),
        "_mention": True,
    }


def _extract_message_body(message: dict) -> str:
    """Return a human-readable body for non-text Instagram message types."""
    if message.get("attachments"):
        types = [a.get("type", "attachment") for a in message["attachments"]]
        labels = {
            "image": "[Photo]",
            "video": "[Video]",
            "audio": "[Audio]",
            "file": "[File]",
        }
        return " ".join(labels.get(t, "[Attachment]") for t in types)
    if message.get("sticker_id"):
        return "[Sticker]"
    if message.get("reactions"):
        r = message["reactions"]
        emoji = r[0].get("emoji", "") if isinstance(r, list) else r.get("emoji", "")
        return f"[Reaction: {emoji}]" if emoji else "[Reaction]"
    if message.get("reply_to"):
        return "[Reply]"
    return ""


def _synthetic_message_id(igsid: str, ts_ms: int) -> str:
    """Fallback message ID when Meta doesn't provide one."""
    return f"synthetic_{igsid}_{ts_ms}"

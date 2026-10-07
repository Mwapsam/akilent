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

    Order: persist contact → persist conversation → persist message → project
    onto the spine (workflows, lead capture, AI) → publish MessageReceived.
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
    sender = entry.get("sender", {})
    igsid = sender.get("id", "")
    if not igsid:
        return

    # Only "message" entries are messages. Read receipts, reactions, postbacks,
    # referrals and edits arrive in the same "messaging" array; treating them as
    # messages put blank customer messages in the inbox and triggered AI replies.
    message = entry.get("message")
    if not message:
        logger.debug(
            "_process_dm_entry: ignoring non-message event keys=%s",
            sorted(k for k in entry if k not in {"sender", "recipient", "timestamp"}),
        )
        return
    if message.get("is_deleted") or message.get("is_unsupported"):
        logger.debug(
            "_process_dm_entry: skipping deleted/unsupported mid=%s", message.get("mid")
        )
        return

    ts_ms = entry.get("timestamp", 0)
    timestamp = (
        datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else timezone.now()
    )

    # Echoes are Meta's copy of a message the business sent. One sent through
    # Akilent is already recorded; one sent from the Instagram app is recorded
    # here, so the inbox shows the whole conversation.
    if message.get("is_echo"):
        _record_echo(instagram_account, entry, message, timestamp)
        return

    _record_inbound_dm(instagram_account, igsid, entry, message, ts_ms, timestamp)


def _record_inbound_dm(
    instagram_account: InstagramBusinessAccount,
    igsid: str,
    entry: dict,
    message: dict,
    ts_ms: int,
    timestamp,
) -> None:
    from apps.conversations.services import record_inbound_instagram_message
    from apps.instagram.services.contacts import resolve_or_create_contact
    from apps.instagram.services.conversations import (
        get_or_create_instagram_conversation,
    )

    message_id = message.get("mid", "")
    message_type, body = describe_message(message)

    # 1. Resolve channel identity -> canonical Contact
    ig_contact = resolve_or_create_contact(instagram_account, igsid)

    # 2. Resolve/create DM conversation + spine (routed once, when created)
    ig_convo, _spine = get_or_create_instagram_conversation(
        ig_contact, instagram_account
    )
    ig_convo.register_inbound(timestamp)

    # 3. Persist the message — get_or_create ensures idempotency across retries.
    # A previous attempt may have created the InstagramMessage but failed before
    # completing the Message spine; always fall through to the projection so the
    # spine is created on the next retry even if ig_message already exists.
    effective_mid = message_id or _synthetic_message_id(igsid, ts_ms)
    ig_message, ig_created = InstagramMessage.objects.get_or_create(
        message_id=effective_mid,
        defaults={
            "conversation": ig_convo,
            "direction": InstagramMessage.Direction.INBOUND,
            "body": body,
            "timestamp": timestamp,
            "metadata": {**entry, "message_type": message_type},
        },
    )
    if not ig_created:
        logger.debug(
            "Duplicate Instagram message %s — ensuring spine exists", effective_mid
        )
        if body and not ig_message.body:
            ig_message.body = body
            ig_message.save(update_fields=["body"])

    # 4. Project onto the canonical spine: Message + Event, then the same
    # forms/workflows/lead capture/AI pipeline WhatsApp uses. Idempotent.
    conversation = record_inbound_instagram_message(
        contact=ig_contact.contact,
        instagram_conversation=ig_convo,
        instagram_message=ig_message,
        enroll_workflows=_automation_events_enabled(),
    )

    # 5. Legacy AutomationRules listen for MessageReceived (new messages only).
    if conversation is not None and ig_contact.contact_id:
        _publish_message_received(
            instagram_account, ig_contact.contact_id, ig_message, message_type
        )


def _record_echo(
    instagram_account: InstagramBusinessAccount,
    entry: dict,
    message: dict,
    timestamp,
) -> None:
    """Record a business message sent outside Akilent (e.g. the Instagram app)."""
    from apps.conversations.services import record_outbound_message
    from apps.instagram.services.contacts import resolve_or_create_contact
    from apps.instagram.services.conversations import (
        get_or_create_instagram_conversation,
    )

    mid = message.get("mid", "")
    customer_igsid = (entry.get("recipient") or {}).get("id", "")
    if not mid or not customer_igsid:
        return
    if InstagramMessage.objects.filter(message_id=mid).exists():
        # Sent through Akilent — already recorded when the send succeeded.
        logger.debug("_record_echo: mid=%s already recorded", mid)
        return

    message_type, body = describe_message(message)
    ig_contact = resolve_or_create_contact(instagram_account, customer_igsid)
    ig_convo, spine = get_or_create_instagram_conversation(
        ig_contact, instagram_account
    )
    ig_message, created = InstagramMessage.objects.get_or_create(
        message_id=mid,
        defaults={
            "conversation": ig_convo,
            "direction": InstagramMessage.Direction.OUTBOUND,
            "status": InstagramMessage.Status.SENT,
            "body": body,
            "timestamp": timestamp,
            "metadata": {**entry, "message_type": message_type},
        },
    )
    if not created:
        return
    ig_convo.register_outbound(timestamp)
    record_outbound_message(
        conversation=spine,
        body=body,
        timestamp=timestamp,
        status="sent",
        metadata={"message_type": message_type, "sent_by": "instagram_app"},
        instagram_message=ig_message,
    )


def _automation_events_enabled() -> bool:
    """Same switch the WhatsApp inbound path uses to decide whether to enroll workflows."""
    try:
        from apps.whatsapp.tasks import _automation_events_enabled as enabled

        return enabled()
    except Exception:
        return False


def _publish_message_received(
    instagram_account, contact_id: int, ig_message, message_type: str
) -> None:
    try:
        from apps.core.events import MessageReceived, dispatcher

        if not _automation_events_enabled():
            return
        dispatcher.publish(
            MessageReceived(
                account_id=instagram_account.account_id,
                contact_id=contact_id,
                message_id=ig_message.message_id,
                channel="instagram",
                body=ig_message.body,
                message_type=message_type,
                occurred_at=ig_message.timestamp,
            )
        )
    except Exception as exc:
        logger.debug("instagram: failed to publish MessageReceived: %s", exc)


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
    if igsid == instagram_account.instagram_business_account_id:
        # The business replying on its own post: not a customer, never a trigger.
        logger.debug("_process_comment_entry: skipping own comment %s", entry.get("id"))
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


_ATTACHMENT_LABELS = {
    "image": "[Photo]",
    "video": "[Video]",
    "audio": "[Audio]",
    "file": "[File]",
    "share": "[Shared post]",
    "ig_reel": "[Reel]",
    "reel": "[Reel]",
    "story_mention": "[Mentioned you in their story]",
}


def describe_message(message: dict) -> tuple[str, str]:
    """``(message_type, body)`` for an Instagram message: its text, or a readable label."""
    text = message.get("text", "") or ""
    reply_to = message.get("reply_to") or {}
    if reply_to.get("story"):
        return "story_reply", f"[Replied to your story] {text}".strip()
    attachments = message.get("attachments") or []
    if attachments:
        types = [a.get("type", "attachment") for a in attachments]
        labels = " ".join(_ATTACHMENT_LABELS.get(t, "[Attachment]") for t in types)
        kind = types[0] if types[0] in _ATTACHMENT_LABELS else "attachment"
        return kind, f"{text} {labels}".strip()
    if text:
        return "text", text
    if message.get("sticker_id"):
        return "sticker", "[Sticker]"
    return "unknown", ""


def _synthetic_message_id(igsid: str, ts_ms: int) -> str:
    """Fallback message ID when Meta doesn't provide one."""
    return f"synthetic_{igsid}_{ts_ms}"

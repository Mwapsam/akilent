"""Inbound Facebook Page webhooks: Messenger messages and Page comments.

Mirrors apps/instagram/services/inbound.py step for step, so a business gets the
same inbox, automation, moderation and comment-to-DM behaviour on both channels.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from django.db import transaction
from django.utils import timezone

from apps.facebook.models.comment import FacebookComment, FacebookCommentThread
from apps.facebook.models.message import FacebookMessage
from apps.facebook.models.webhook import FacebookWebhookEventLog

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Messenger
# ---------------------------------------------------------------------------


def handle_dm(event: FacebookWebhookEventLog, conn) -> None:
    """Process an inbound Messenger webhook event for the connected Page ``conn``."""
    for entry in _extract_messaging(event.raw_payload):
        _process_dm_entry(conn, entry)


def _process_dm_entry(conn, entry: dict) -> None:
    sender_id = (entry.get("sender") or {}).get("id", "")
    recipient_id = (entry.get("recipient") or {}).get("id", "")
    if not sender_id:
        return

    message = entry.get("message")
    postback = entry.get("postback")
    if postback and not message:
        # A tapped button (Get Started, a persistent-menu item): the customer's answer,
        # recorded as a message whose quick-reply payload routes waiting workflows.
        message = {
            "mid": postback.get("mid", ""),
            "text": postback.get("title", ""),
            "quick_reply": {"payload": postback.get("payload", "")},
            "_postback": True,
        }
    if not message:
        # Deliveries, reads, reactions, referrals: not messages.
        logger.debug(
            "facebook _process_dm_entry: ignoring keys=%s",
            sorted(k for k in entry if k not in {"sender", "recipient", "timestamp"}),
        )
        return
    if message.get("is_deleted") or message.get("is_unsupported"):
        return

    ts_ms = entry.get("timestamp", 0)
    timestamp = (
        datetime.fromtimestamp(ts_ms / 1000, tz=UTC) if ts_ms else timezone.now()
    )

    # The Page must be a party: the sender of an echo, the recipient of a customer's
    # message. Anything else is filed under the wrong business.
    business_id = sender_id if message.get("is_echo") else recipient_id
    if business_id and business_id != conn.page_id:
        logger.warning(
            "facebook _process_dm_entry: message for page=%s on connection=%s",
            business_id,
            conn.pk,
        )
        return

    if message.get("is_echo"):
        _record_echo(conn, entry, message, timestamp)
        return

    _record_inbound_dm(conn, sender_id, entry, message, ts_ms, timestamp)


def _record_inbound_dm(conn, psid, entry, message, ts_ms, timestamp) -> None:
    from apps.conversations.services import record_inbound_facebook_message
    from apps.facebook.services.contacts import resolve_or_create_contact
    from apps.facebook.services.conversations import (
        get_or_create_facebook_conversation,
    )

    message_type, body = describe_message(message)
    fb_contact = resolve_or_create_contact(conn, psid)
    fb_convo, _spine = get_or_create_facebook_conversation(fb_contact, conn.page_id)
    fb_convo.register_inbound(timestamp)

    mid = message.get("mid") or f"synthetic_{psid}_{ts_ms}"
    fb_message, created = FacebookMessage.objects.get_or_create(
        message_id=mid,
        defaults={
            "conversation": fb_convo,
            "direction": FacebookMessage.Direction.INBOUND,
            "body": body,
            "timestamp": timestamp,
            "metadata": {**entry, "message": message, "message_type": message_type},
            **media_fields(message),
        },
    )
    if created and fb_message.media_source_url:
        from apps.facebook.tasks import download_facebook_media

        transaction.on_commit(lambda: download_facebook_media.delay(fb_message.pk))

    conversation = record_inbound_facebook_message(
        contact=fb_contact.contact,
        facebook_conversation=fb_convo,
        facebook_message=fb_message,
        enroll_workflows=_automation_events_enabled(),
    )
    if conversation is not None and fb_contact.contact_id:
        _publish_message_received(conn, fb_contact.contact_id, fb_message, message_type)


def _record_echo(conn, entry: dict, message: dict, timestamp) -> None:
    """Record a Page message sent outside Akilent (Meta Business Suite, Page inbox)."""
    from apps.conversations.services import record_outbound_message
    from apps.facebook.services.contacts import resolve_or_create_contact
    from apps.facebook.services.conversations import (
        get_or_create_facebook_conversation,
    )

    mid = message.get("mid", "")
    customer_psid = (entry.get("recipient") or {}).get("id", "")
    if not mid or not customer_psid:
        return
    if FacebookMessage.objects.filter(message_id=mid).exists():
        return  # sent through Akilent: recorded when the send succeeded

    message_type, body = describe_message(message)
    fb_contact = resolve_or_create_contact(conn, customer_psid)
    fb_convo, spine = get_or_create_facebook_conversation(fb_contact, conn.page_id)
    fb_message, created = FacebookMessage.objects.get_or_create(
        message_id=mid,
        defaults={
            "conversation": fb_convo,
            "direction": FacebookMessage.Direction.OUTBOUND,
            "status": FacebookMessage.Status.SENT,
            "body": body,
            "timestamp": timestamp,
            "metadata": {**entry, "message_type": message_type},
        },
    )
    if not created:
        return
    fb_convo.register_outbound(timestamp)
    record_outbound_message(
        conversation=spine,
        body=body,
        timestamp=timestamp,
        status="sent",
        metadata={"message_type": message_type, "sent_by": "facebook_app"},
        facebook_message=fb_message,
    )


def _automation_events_enabled() -> bool:
    from apps.instagram.services.inbound import _automation_events_enabled as enabled

    return enabled()


def _publish_message_received(conn, contact_id: int, fb_message, message_type) -> None:
    try:
        from apps.core.events import MessageReceived, dispatcher

        if not _automation_events_enabled():
            return
        dispatcher.publish(
            MessageReceived(
                account_id=conn.account_id,
                contact_id=contact_id,
                message_id=fb_message.message_id,
                channel="facebook",
                body=fb_message.body,
                message_type=message_type,
                occurred_at=fb_message.timestamp,
            )
        )
    except Exception as exc:
        logger.debug("facebook: failed to publish MessageReceived: %s", exc)


# ---------------------------------------------------------------------------
# Page comments (feed webhooks)
# ---------------------------------------------------------------------------


def handle_comment(event: FacebookWebhookEventLog, conn) -> None:
    """Process the comment changes in a feed webhook event.

    A comment is stored as a thread + comment, moderated, and checked against the
    comment triggers. It does not open a conversation: only a private reply does.
    """
    for value in _extract_comment_changes(event.raw_payload):
        verb = value.get("verb", "")
        if verb == "add":
            _process_new_comment(conn, value)
        elif verb == "edited":
            FacebookComment.objects.filter(
                thread__account_id=conn.account_id, comment_id=value.get("comment_id")
            ).update(body=value.get("message", ""))
        elif verb in {"remove", "hide", "unhide"}:
            _apply_visibility(conn, value.get("comment_id", ""), verb)


def _process_new_comment(conn, value: dict) -> None:
    from apps.conversations.intent import detect_buying_intent
    from apps.facebook.services.contacts import resolve_or_create_contact

    commenter = value.get("from") or {}
    commenter_id = commenter.get("id", "")
    if not commenter_id:
        return
    if commenter_id == conn.page_id:
        # The Page commenting on its own post: not a customer, never a trigger.
        return

    comment_id = value.get("comment_id", "")
    post_id = value.get("post_id", "")
    body = value.get("message", "") or ""
    raw_parent = value.get("parent_id", "") or ""
    # A top-level comment's parent is the post itself.
    parent_id = "" if raw_parent in {"", post_id} else raw_parent
    created_time = value.get("created_time")
    try:
        timestamp = (
            datetime.fromtimestamp(int(created_time), tz=UTC)
            if created_time
            else timezone.now()
        )
    except (TypeError, ValueError):
        timestamp = timezone.now()

    fb_contact = resolve_or_create_contact(
        conn, commenter_id, name=commenter.get("name", "")
    )
    root_id = parent_id or comment_id
    thread, _ = FacebookCommentThread.objects.get_or_create(
        account_id=conn.account_id,
        comment_id=root_id,
        defaults={
            "page_id": conn.page_id,
            "post_id": post_id,
            "facebook_contact": fb_contact,
            "body": body if not parent_id else "",
            "received_at": timestamp,
        },
    )
    if thread.content_variant_id is None:
        link_thread_to_content(thread, value)

    comment, _ = FacebookComment.objects.get_or_create(
        thread=thread,
        comment_id=comment_id,
        defaults={
            "facebook_contact": fb_contact,
            "parent_comment_id": parent_id,
            "body": body,
            "direction": FacebookComment.Direction.INBOUND,
            "timestamp": timestamp,
        },
    )

    from apps.facebook.services.moderation import moderate_comment

    moderate_comment(comment, conn)

    # Root comments only, as on Instagram: a trigger answers a comment, not a reply.
    if not parent_id and body:
        from apps.facebook.services.triggers import evaluate_triggers

        evaluate_triggers(thread, body, conn)

        intent_phrase = detect_buying_intent(body)
        if intent_phrase and not thread.intent:
            thread.intent = intent_phrase
            thread.save(update_fields=["intent"])


def _apply_visibility(conn, comment_id: str, verb: str) -> None:
    """Someone hid, unhid or removed a comment outside Akilent: mirror it."""
    comments = FacebookComment.objects.filter(
        thread__account_id=conn.account_id, comment_id=comment_id
    )
    if verb == "remove":
        comments.update(moderation_state=FacebookComment.ModerationState.DELETED)
    elif verb == "hide":
        comments.update(
            is_hidden=True, moderation_state=FacebookComment.ModerationState.HIDDEN
        )
    else:
        comments.filter(moderation_state=FacebookComment.ModerationState.HIDDEN).update(
            is_hidden=False, moderation_state=FacebookComment.ModerationState.APPROVED
        )
        comments.update(is_hidden=False)


def link_thread_to_content(thread: FacebookCommentThread, value: dict | None = None):
    """Point a comment thread at the Content Studio channel that published its post.

    A Page post's id is "{page}_{post}". A photo or video post is published as an
    object with its own id; the metrics collector caches the post id it resolved.
    """
    from django.db.models import Q

    from apps.publishing.models import ContentPostVariant

    if not thread.post_id:
        return
    ids = {thread.post_id, thread.post_id.partition("_")[2]}
    value = value or {}
    for key in ("photo_id", "video_id"):
        if value.get(key):
            ids.add(str(value[key]))
    ids.discard("")
    variant = (
        ContentPostVariant.objects.filter(
            platform=ContentPostVariant.Platform.FACEBOOK,
            post__account_id=thread.account_id,
        )
        .filter(Q(publication_id__in=ids) | Q(metadata__fb_post_id=thread.post_id))
        .first()
    )
    if variant is not None:
        FacebookCommentThread.objects.filter(pk=thread.pk).update(
            content_variant=variant
        )
        thread.content_variant = variant


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_messaging(payload: dict) -> list[dict]:
    messaging = []
    for entry in payload.get("entry", []):
        messaging.extend(entry.get("messaging", []))
    return messaging


def _extract_comment_changes(payload: dict) -> list[dict]:
    values = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            if change.get("field") == "feed" and value.get("item") == "comment":
                values.append(value)
    return values


_ATTACHMENT_LABELS = {
    "image": "[Photo]",
    "video": "[Video]",
    "audio": "[Voice message]",
    "file": "[File]",
    "location": "[Location]",
    "fallback": "[Link]",
    "template": "[Message]",
}

_DOWNLOADABLE = {"image", "video", "audio", "file"}


def media_fields(message: dict) -> dict:
    """FacebookMessage media fields for the message's first downloadable attachment."""
    for attachment in message.get("attachments") or []:
        kind = attachment.get("type", "")
        url = (attachment.get("payload") or {}).get("url", "")
        if kind in _DOWNLOADABLE and url:
            return {"media_type": kind, "media_source_url": url}
    return {}


def describe_message(message: dict) -> tuple[str, str]:
    """``(message_type, body)``: the message's text, or a readable label."""
    text = message.get("text", "") or ""
    if message.get("_postback"):
        return "postback", text
    attachments = message.get("attachments") or []
    if attachments:
        types = [a.get("type", "attachment") for a in attachments]
        if message.get("sticker_id") and types[0] == "image":
            return "sticker", "[Sticker]"
        labels = " ".join(_ATTACHMENT_LABELS.get(t, "[Attachment]") for t in types)
        kind = types[0] if types[0] in _ATTACHMENT_LABELS else "attachment"
        return kind, f"{text} {labels}".strip()
    if text:
        return "text", text
    return "unknown", ""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.instagram.models.account import InstagramBusinessAccount
from apps.instagram.models.message import InstagramMessage, OutboundMessage

logger = logging.getLogger(__name__)

# A send that has been "sending" this long was interrupted (worker killed mid-call):
# Meta may or may not have delivered it, so it is never retried automatically.
_SENDING_STALE = timedelta(minutes=10)
_DRAIN_BATCH = 50


def enqueue_reply(
    instagram_account: InstagramBusinessAccount,
    recipient_igsid: str,
    body: str,
    *,
    action_type: str,
    idempotency_key: str,
    media=None,
    quick_replies: list | None = None,
) -> OutboundMessage | None:
    """
    Create an OutboundMessage record (outbox-first pattern).

    Returns the new OutboundMessage, or None if the idempotency key already
    exists (meaning this send was already enqueued or sent — safe to ignore).
    ``quick_replies`` is a list of IG quick-reply chip dicts (A workstream).
    """
    try:
        with transaction.atomic():
            msg = OutboundMessage.objects.create(
                instagram_account=instagram_account,
                idempotency_key=idempotency_key,
                recipient_igsid=recipient_igsid,
                action_type=action_type,
                body=body,
                media_path=media.path if media else "",
                media_mime_type=media.mime if media else "",
                media_kind=media.kind if media else "",
                quick_replies=quick_replies or [],
            )
        return msg
    except IntegrityError:
        logger.debug(
            "OutboundMessage already exists for idempotency_key=%s", idempotency_key
        )
        return None


def send_outbound(outbound: OutboundMessage) -> bool:
    """
    Attempt to send one OutboundMessage via the Meta provider.

    Updates OutboundMessage status from the SendResult. On success the message
    is also recorded as an outbound ``InstagramMessage`` and put on the
    conversation's inbox thread. Returns True if sent successfully.
    """
    from apps.instagram.providers.meta import MetaInstagramProvider

    if outbound.status == OutboundMessage.Status.SENT:
        return True  # already sent — idempotent

    instagram_account = outbound.instagram_account
    if not instagram_account.access_token:
        logger.error(
            "send_outbound: instagram_account %s has no access_token",
            instagram_account.pk,
        )
        outbound.mark_failed(
            "Instagram isn't connected — reconnect it in Settings.", terminal=True
        )
        _notify_automation_failed(outbound)
        return False
    provider = MetaInstagramProvider(
        access_token=instagram_account.access_token,
        instagram_account_id=instagram_account.instagram_business_account_id,
    )

    eligibility = provider.can_send(outbound.recipient_igsid, outbound.action_type)
    if not eligibility.eligible:
        outbound.mark_failed(
            f"Not eligible to send: {eligibility.reason}", terminal=True
        )
        _notify_automation_failed(outbound)
        return False

    outbound.status = OutboundMessage.Status.SENDING
    # updated_at is what the drain's stale-"sending" check measures from.
    outbound.save(update_fields=["status", "updated_at"])

    if outbound.action_type == OutboundMessage.ActionType.PRIVATE_REPLY:
        # private_reply expects a comment_id, not an igsid; the caller must
        # pass the comment_id as recipient_igsid for private reply actions.
        result = provider.private_reply(outbound.recipient_igsid, outbound.body)
    elif outbound.media_path:
        result = provider.send_attachment(
            outbound.recipient_igsid,
            _ATTACHMENT_TYPE.get(outbound.media_kind, "file"),
            signed_media_url(outbound.media_path, outbound.media_mime_type),
        )
    elif outbound.quick_replies:
        result = provider.send_quick_reply(
            outbound.recipient_igsid, outbound.body, outbound.quick_replies
        )
    else:
        result = provider.send_message(outbound.recipient_igsid, outbound.body)

    if result.success:
        outbound.mark_sent(result.provider_message_id)
        try:
            record_sent_message(outbound)
        except Exception:
            # The customer has the message; failing to mirror it must not
            # turn a delivered send into a retry (and a duplicate).
            logger.exception(
                "send_outbound: could not record sent message outbound=%s", outbound.pk
            )
        return True

    if result.token_invalid and not instagram_account.token_expired:
        InstagramBusinessAccount.objects.filter(pk=instagram_account.pk).update(
            token_expired=True
        )
        instagram_account.token_expired = True
        logger.warning(
            "send_outbound: token for instagram_account %s is invalid — reconnect needed",
            instagram_account.pk,
        )
    outbound.mark_failed(_readable_error(result), terminal=result.terminal)
    if outbound.status == OutboundMessage.Status.FAILED:
        _notify_automation_failed(outbound)
    return False


def _notify_automation_failed(outbound: OutboundMessage) -> None:
    """Call the automation reconciliation hook when an outbound message reaches FAILED."""
    try:
        from apps.automation.integrations.instagram import mark_outbound_message_failed

        mark_outbound_message_failed(outbound)
    except Exception:
        logger.exception(
            "send_outbound: automation hook raised outbound=%s", outbound.pk
        )


def _readable_error(result) -> str:
    """Meta's error, with what to do about it when we know."""
    if result.error_code == 100 and "cannot be found" in (result.error or ""):
        # What Meta answers when the app lacks Advanced Access to
        # instagram_business_manage_messages and the customer has no role on it.
        return (
            "Instagram didn't deliver this: until Meta approves Akilent's Instagram "
            "messaging permission, replies only reach accounts added as testers "
            f"on the Akilent app. ({result.error})"
        )
    return result.error


# Our media kinds -> Instagram Send API attachment types.
_ATTACHMENT_TYPE = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "document": "file",
}
_MEDIA_LINK_SALT = "instagram.outbound-media"
MEDIA_LINK_MAX_AGE = 3600  # seconds; Meta fetches the file within moments of the send


def signed_media_url(path: str, mime: str) -> str:
    """A public, expiring link to one stored file, for Meta to fetch an attachment from.

    Signed so it can't be forged or altered, and short-lived so it stops working
    soon after the send.
    """
    from django.conf import settings
    from django.core import signing
    from django.urls import reverse

    token = signing.dumps({"p": path, "m": mime}, salt=_MEDIA_LINK_SALT, compress=True)
    return settings.SITE_URL.rstrip("/") + reverse(
        "instagram-outbound-media", args=[token]
    )


def resolve_media_token(token: str) -> tuple[str, str] | None:
    """``(path, mime)`` for a valid, unexpired media link token, else None."""
    from django.core import signing

    try:
        data = signing.loads(token, salt=_MEDIA_LINK_SALT, max_age=MEDIA_LINK_MAX_AGE)
    except signing.BadSignature:
        return None
    return data.get("p", ""), data.get("m", "")


def outbound_message_id(outbound: OutboundMessage) -> str:
    """The InstagramMessage id for a sent outbox row (Meta's id, else a local one)."""
    return outbound.provider_message_id or f"outbound:{outbound.pk}"


def sender_of(outbound: OutboundMessage) -> str:
    """Who sent it when it wasn't a person — same convention as WhatsApp's ``_sender_of``."""
    key = outbound.idempotency_key or ""
    if key.startswith("ai-auto:"):
        return "ai"
    if key.startswith(("wf:", "private_reply:")):
        return "automation"
    return ""


def record_sent_message(outbound: OutboundMessage):
    """Record a sent outbox row as an outbound InstagramMessage + inbox Message.

    Idempotent: a second call returns the existing inbox Message. The
    InstagramMessage also lets the webhook's echo of this send be recognised as
    already recorded. Returns the spine ``Message``, or None if the customer
    can't be resolved.
    """
    from apps.conversations.models import Message
    from apps.conversations.services import record_outbound_message

    mid = outbound_message_id(outbound)
    existing = (
        Message.objects.filter(instagram_message__message_id=mid)
        .select_related("instagram_message")
        .first()
    )
    if existing is not None:
        return existing

    ig_contact = _recipient_contact(outbound)
    if ig_contact is None or ig_contact.contact_id is None:
        logger.warning(
            "record_sent_message: no Instagram contact for outbound=%s", outbound.pk
        )
        return None

    from apps.instagram.services.conversations import (
        get_or_create_instagram_conversation,
    )

    sent_at = outbound.sent_at or timezone.now()
    ig_convo, spine = get_or_create_instagram_conversation(
        ig_contact, outbound.instagram_account
    )
    ig_message, _ = InstagramMessage.objects.get_or_create(
        message_id=mid,
        defaults={
            "conversation": ig_convo,
            "direction": InstagramMessage.Direction.OUTBOUND,
            "status": InstagramMessage.Status.SENT,
            "body": outbound.body,
            "timestamp": sent_at,
            "metadata": {
                "outbound_id": outbound.pk,
                "action_type": outbound.action_type,
            },
            "media_type": outbound.media_kind,
            "media_mime_type": outbound.media_mime_type,
            "media_file": outbound.media_path or None,
        },
    )
    ig_convo.register_outbound(sent_at)
    metadata = {"message_type": outbound.media_kind or "text"}
    who = sender_of(outbound)
    if who:
        metadata["sent_by"] = who
    message, _ = record_outbound_message(
        conversation=spine,
        body=outbound.body,
        timestamp=sent_at,
        status="sent",
        metadata=metadata,
        instagram_message=ig_message,
    )
    return message


def _recipient_contact(outbound: OutboundMessage):
    from apps.instagram.models.comment import CommentThread
    from apps.instagram.models.contact import InstagramContact

    if outbound.action_type == OutboundMessage.ActionType.PRIVATE_REPLY:
        thread = (
            CommentThread.objects.filter(
                instagram_account=outbound.instagram_account,
                comment_id=outbound.recipient_igsid,
            )
            .select_related("instagram_contact")
            .first()
        )
        return thread.instagram_contact if thread else None
    return InstagramContact.objects.filter(
        account_id=outbound.instagram_account.account_id,
        instagram_scoped_id=outbound.recipient_igsid,
    ).first()


def drain_outbox(now=None) -> dict:
    """Retry queued sends whose backoff has elapsed; give up on interrupted ones.

    ``mark_failed`` schedules ``next_attempt_at`` for transient failures — this is
    what actually retries them.
    """
    now = now or timezone.now()
    stale = OutboundMessage.objects.filter(
        status=OutboundMessage.Status.SENDING,
        updated_at__lt=now - _SENDING_STALE,
    ).update(
        status=OutboundMessage.Status.UNCONFIRMED,
        last_error="Interrupted while sending — it may or may not have been delivered.",
    )

    sent = failed = 0
    with transaction.atomic():
        due_ids = list(
            OutboundMessage.objects.select_for_update(skip_locked=True)
            .filter(status=OutboundMessage.Status.QUEUED)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .order_by("created_at")
            .values_list("pk", flat=True)[:_DRAIN_BATCH]
        )
        # Claim them inside the lock so a concurrent drain skips them.
        OutboundMessage.objects.filter(pk__in=due_ids).update(
            status=OutboundMessage.Status.SENDING, updated_at=now
        )

    for outbound in OutboundMessage.objects.filter(pk__in=due_ids).select_related(
        "instagram_account"
    ):
        if send_outbound(outbound):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "unconfirmed": stale}

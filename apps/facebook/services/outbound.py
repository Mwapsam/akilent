"""Facebook outbox: every Messenger reply, private reply and comment goes through here.

Mirrors apps/instagram/services/outbound.py. Outbox-first (the row exists before the
API call), idempotent on ``idempotency_key``, and the reply windows are enforced in
``send_outbound``, which every path calls: inbox, workflows, AI, the outbox drain.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.facebook.models.message import FacebookMessage, FacebookOutboundMessage

logger = logging.getLogger(__name__)

_SENDING_STALE = timedelta(minutes=10)
_DRAIN_BATCH = 50

WINDOW_CLOSED_ERROR = (
    "Not sent: Messenger only allows replies within 24 hours of the customer's "
    "last message."
)
# A private reply opened the thread and the customer hasn't written back: Messenger
# allows nothing more until they do (the Human Agent tag needs a customer message too).
AWAITING_REPLY_ERROR = (
    "Not sent: you've sent a private reply to their comment. Messenger lets you "
    "write again once they reply."
)
HUMAN_WINDOW_CLOSED_ERROR = (
    "Not sent: it's more than 7 days since the customer's last message, so Messenger "
    "no longer allows a reply."
)
NOT_CONNECTED_ERROR = "Facebook isn't connected — reconnect your Page in Settings."

# Our media kinds -> Messenger attachment types.
_ATTACHMENT_TYPE = {
    "image": "image",
    "video": "video",
    "audio": "audio",
    "document": "file",
}


def enqueue_reply(
    conn,
    recipient_id: str,
    body: str,
    *,
    action_type: str,
    idempotency_key: str,
    media=None,
    quick_replies: list | None = None,
    human_agent: bool = False,
) -> FacebookOutboundMessage | None:
    """Create the outbox row. None when the idempotency key already exists."""
    try:
        with transaction.atomic():
            return FacebookOutboundMessage.objects.create(
                account_id=conn.account_id,
                page_id=conn.page_id,
                idempotency_key=idempotency_key,
                recipient_id=recipient_id,
                action_type=action_type,
                body=body,
                media_path=media.path if media else "",
                media_mime_type=media.mime if media else "",
                media_kind=media.kind if media else "",
                quick_replies=quick_replies or [],
                human_agent=human_agent,
            )
    except IntegrityError:
        logger.debug("Facebook outbox row exists for key=%s", idempotency_key)
        return None


def _connection_for(outbound: FacebookOutboundMessage):
    """The Page connection this row was queued for, while it is still connected."""
    from apps.facebook.models.page import active_page

    conn = active_page(outbound.account)
    if conn is None or conn.page_id != outbound.page_id:
        return None
    return conn


def send_outbound(outbound: FacebookOutboundMessage) -> bool:
    """Send one outbox row. True when Meta accepted it."""
    from apps.facebook.providers.meta import provider_for

    A = FacebookOutboundMessage.ActionType
    if outbound.status == FacebookOutboundMessage.Status.SENT:
        return True

    from apps.core.channels import channel_state

    if channel_state("facebook") != "enabled":
        # Switched off: back to QUEUED (the drain may have marked it SENDING), so it goes
        # when Facebook is back on rather than going stale.
        FacebookOutboundMessage.objects.filter(pk=outbound.pk).update(
            status=FacebookOutboundMessage.Status.QUEUED
        )
        return False

    conn = _connection_for(outbound)
    if conn is None:
        outbound.mark_failed(NOT_CONNECTED_ERROR, terminal=True)
        _notify_automation_failed(outbound)
        return False

    tag = False
    if outbound.action_type == A.DM_REPLY:
        window = dm_window(outbound.account_id, outbound.page_id, outbound.recipient_id)
        if window == "open":
            tag = False
        elif window == "human" and outbound.human_agent:
            tag = True
        else:
            outbound.mark_failed(
                AWAITING_REPLY_ERROR
                if window == "awaiting"
                else HUMAN_WINDOW_CLOSED_ERROR
                if outbound.human_agent
                else WINDOW_CLOSED_ERROR,
                terminal=True,
            )
            _notify_automation_failed(outbound)
            return False

    outbound.status = FacebookOutboundMessage.Status.SENDING
    outbound.save(update_fields=["status", "updated_at"])

    provider = provider_for(conn)
    if outbound.action_type == A.PRIVATE_REPLY:
        result = provider.private_reply(outbound.recipient_id, outbound.body)
    elif outbound.action_type == A.COMMENT_REPLY:
        result = provider.reply_to_comment(outbound.recipient_id, outbound.body)
    elif outbound.action_type == A.POST_COMMENT:
        result = provider.comment_on_post(outbound.recipient_id, outbound.body)
    elif outbound.media_path:
        from apps.instagram.services.outbound import signed_media_url

        result = provider.send_attachment(
            outbound.recipient_id,
            _ATTACHMENT_TYPE.get(outbound.media_kind, "file"),
            signed_media_url(outbound.media_path, outbound.media_mime_type),
            human_agent=tag,
        )
    else:
        result = provider.send_message(
            outbound.recipient_id,
            outbound.body,
            human_agent=tag,
            quick_replies=outbound.quick_replies or None,
        )

    if result.success:
        recipient = getattr(result, "recipient_id", "")
        if (
            not recipient
            and outbound.action_type == A.PRIVATE_REPLY
            and result.provider_message_id
        ):
            # The commenter's Messenger thread must be keyed on their PSID, or their
            # reply opens a second conversation the business can't answer from here.
            recipient = provider.message_recipient(result.provider_message_id)
        if recipient and recipient != outbound.resolved_recipient_id:
            outbound.resolved_recipient_id = recipient
            FacebookOutboundMessage.objects.filter(pk=outbound.pk).update(
                resolved_recipient_id=recipient
            )
        outbound.mark_sent(result.provider_message_id)
        try:
            record_sent(outbound)
        except Exception:
            # The customer has it; failing to mirror it must not turn into a retry.
            logger.exception(
                "facebook send_outbound: could not record outbound=%s", outbound.pk
            )
        return True

    if result.token_invalid:
        from apps.publishing.models import FacebookPageConnection

        FacebookPageConnection.objects.filter(pk=conn.pk).update(
            status=FacebookPageConnection.Status.EXPIRED
        )
        logger.warning("facebook send_outbound: Page token for %s is invalid", conn.pk)
    outbound.mark_failed(_readable_error(result), terminal=result.terminal)
    if outbound.status == FacebookOutboundMessage.Status.FAILED:
        _notify_automation_failed(outbound)
    return False


def dm_window(account_id: int, page_id: str, psid: str) -> str:
    """ "open" (inside 24h), "human" (24h–7 days: a person's reply, tagged), "closed",
    or "awaiting" (the customer has never messaged the Page, e.g. only got a private reply).

    Measured from the customer's last message to this Page.
    """
    from apps.facebook.models import FacebookConversation

    thread = (
        FacebookConversation.objects.filter(
            account_id=account_id, page_id=page_id, facebook_contact__psid=psid
        )
        .order_by("-last_inbound_at")
        .first()
    )
    if thread is None or thread.last_inbound_at is None:
        return "awaiting"
    if thread.window_is_open:
        return "open"
    if thread.human_agent_window_is_open:
        return "human"
    return "closed"


def _readable_error(result) -> str:
    if result.error_code in {10, 200} or (
        result.error_code == 100 and "permission" in (result.error or "").lower()
    ):
        return (
            "Facebook didn't deliver this: until Meta approves Akilent's Messenger "
            "permission, replies only reach people with a role on the Akilent app. "
            f"({result.error})"
        )
    return result.error


def _notify_automation_failed(outbound: FacebookOutboundMessage) -> None:
    try:
        from apps.automation.integrations.facebook import mark_outbound_message_failed

        mark_outbound_message_failed(outbound)
    except Exception:
        logger.exception(
            "facebook send_outbound: automation hook raised %s", outbound.pk
        )


def outbound_message_id(outbound: FacebookOutboundMessage) -> str:
    return outbound.provider_message_id or f"outbound:fb:{outbound.pk}"


def sender_of(outbound: FacebookOutboundMessage) -> str:
    """Who sent it when it wasn't a person (same convention as Instagram's)."""
    key = outbound.idempotency_key or ""
    if key.startswith("ai-auto:"):
        return "ai"
    if key.startswith(("wf:", "private_reply:", "form:")):
        return "automation"
    return ""


def record_sent(outbound: FacebookOutboundMessage):
    """Mirror a sent row where people look for it.

    A public comment, or a comment on a post: an outbound comment on its thread (the
    Comments view). A Messenger message or private reply: the inbox thread. A public
    reply sent by a trigger also lands on the inbox thread, as on Instagram.
    """
    A = FacebookOutboundMessage.ActionType
    if outbound.action_type in {A.COMMENT_REPLY, A.POST_COMMENT}:
        _record_outbound_comment(outbound)
        if not (
            outbound.action_type == A.COMMENT_REPLY
            and outbound.idempotency_key.startswith("private_reply:")
        ):
            return None
    return record_sent_message(outbound)


def _record_outbound_comment(outbound: FacebookOutboundMessage) -> None:
    from apps.facebook.models.comment import FacebookComment, FacebookCommentThread

    if outbound.action_type != FacebookOutboundMessage.ActionType.COMMENT_REPLY:
        return  # a comment on a post has no visitor thread; the outbox row is its record
    thread = FacebookCommentThread.objects.filter(
        account_id=outbound.account_id, comment_id=outbound.recipient_id
    ).first()
    if thread is None:
        comment = (
            FacebookComment.objects.filter(
                thread__account_id=outbound.account_id, comment_id=outbound.recipient_id
            )
            .select_related("thread")
            .first()
        )
        thread = comment.thread if comment else None
    if thread is None:
        return
    FacebookComment.objects.get_or_create(
        thread=thread,
        comment_id=outbound_message_id(outbound),
        defaults={
            "facebook_contact_id": thread.facebook_contact_id,
            "parent_comment_id": outbound.recipient_id,
            "body": outbound.body,
            "direction": FacebookComment.Direction.OUTBOUND,
            "moderation_state": FacebookComment.ModerationState.APPROVED,
            "timestamp": outbound.sent_at or timezone.now(),
        },
    )


def record_sent_message(outbound: FacebookOutboundMessage):
    """Record a sent row as an outbound FacebookMessage + inbox Message. Idempotent."""
    from apps.conversations.inbox_keys import sender_metadata
    from apps.conversations.models import Message
    from apps.conversations.services import record_outbound_message
    from apps.facebook.services.conversations import (
        get_or_create_facebook_conversation,
    )

    mid = outbound_message_id(outbound)
    existing = Message.objects.filter(facebook_message__message_id=mid).first()
    if existing is not None:
        # The echo can arrive first and record it as sent from the Page inbox; it was
        # ours, so put our sender and the inbox's key on it.
        merged = {**existing.metadata, **sender_metadata(outbound.idempotency_key)}
        if merged.get("sent_by") == "facebook_app":
            who = sender_of(outbound)
            if who:
                merged["sent_by"] = who
            else:
                merged.pop("sent_by")
        if merged != existing.metadata:
            existing.metadata = merged
            existing.save(update_fields=["metadata"])
        return existing

    fb_contact = _recipient_contact(outbound)
    if fb_contact is None or fb_contact.contact_id is None:
        logger.warning(
            "facebook record_sent_message: no contact for outbound=%s", outbound.pk
        )
        return None

    sent_at = outbound.sent_at or timezone.now()
    fb_convo, spine = get_or_create_facebook_conversation(fb_contact, outbound.page_id)
    fb_message, _ = FacebookMessage.objects.get_or_create(
        message_id=mid,
        defaults={
            "conversation": fb_convo,
            "direction": FacebookMessage.Direction.OUTBOUND,
            "status": FacebookMessage.Status.SENT,
            "body": outbound.body,
            "timestamp": sent_at,
            "metadata": {
                "outbound_id": outbound.pk,
                "action_type": outbound.action_type,
                "human_agent": outbound.human_agent,
            },
            "media_type": outbound.media_kind,
            "media_mime_type": outbound.media_mime_type,
            "media_file": outbound.media_path or None,
        },
    )
    fb_convo.register_outbound(sent_at)
    metadata = {"message_type": outbound.media_kind or "text"}
    who = sender_of(outbound)
    if who:
        metadata["sent_by"] = who
    metadata.update(sender_metadata(outbound.idempotency_key))
    message, _ = record_outbound_message(
        conversation=spine,
        body=outbound.body,
        timestamp=sent_at,
        status="sent",
        metadata=metadata,
        facebook_message=fb_message,
    )
    return message


def _recipient_contact(outbound: FacebookOutboundMessage):
    from apps.facebook.models.contact import FacebookContact

    A = FacebookOutboundMessage.ActionType
    if outbound.action_type in {A.PRIVATE_REPLY, A.COMMENT_REPLY}:
        commenter = _commenter(outbound)
        if outbound.action_type == A.PRIVATE_REPLY and commenter is not None:
            return messenger_contact(commenter, outbound.resolved_recipient_id)
        return commenter
    return FacebookContact.objects.filter(
        account_id=outbound.account_id, psid=outbound.recipient_id
    ).first()


def messenger_contact(commenter, psid: str):
    """The commenter's Messenger identity once a private reply named their PSID.

    Both identities point at the same customer, so the comment and the Messenger
    thread it opened are one person in Akilent.
    """
    from apps.facebook.models.contact import FacebookContact

    if not psid or psid == commenter.psid:
        return commenter
    fb_contact, created = FacebookContact.objects.get_or_create(
        account_id=commenter.account_id,
        psid=psid,
        defaults={"name": commenter.name, "contact_id": commenter.contact_id},
    )
    if not created and fb_contact.contact_id is None and commenter.contact_id:
        fb_contact.contact_id = commenter.contact_id
        fb_contact.save(update_fields=["contact"])
    return fb_contact


def _commenter(outbound: FacebookOutboundMessage):
    """Who wrote the comment the row answers (a root comment or a reply in a thread)."""
    from apps.facebook.models.comment import FacebookComment, FacebookCommentThread

    thread = (
        FacebookCommentThread.objects.filter(
            account_id=outbound.account_id, comment_id=outbound.recipient_id
        )
        .select_related("facebook_contact")
        .first()
    )
    if thread is not None:
        return thread.facebook_contact
    comment = (
        FacebookComment.objects.filter(
            thread__account_id=outbound.account_id, comment_id=outbound.recipient_id
        )
        .select_related("facebook_contact")
        .first()
    )
    return comment.facebook_contact if comment else None


def drain_outbox(now=None) -> dict:
    """Retry queued sends whose backoff has elapsed; give up on interrupted ones."""
    now = now or timezone.now()
    stale = FacebookOutboundMessage.objects.filter(
        status=FacebookOutboundMessage.Status.SENDING,
        updated_at__lt=now - _SENDING_STALE,
    ).update(
        status=FacebookOutboundMessage.Status.UNCONFIRMED,
        last_error="Interrupted while sending — it may or may not have been delivered.",
    )

    sent = failed = 0
    with transaction.atomic():
        due_ids = list(
            FacebookOutboundMessage.objects.select_for_update(skip_locked=True)
            .filter(status=FacebookOutboundMessage.Status.QUEUED)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .order_by("created_at")
            .values_list("pk", flat=True)[:_DRAIN_BATCH]
        )
        FacebookOutboundMessage.objects.filter(pk__in=due_ids).update(
            status=FacebookOutboundMessage.Status.SENDING, updated_at=now
        )

    for outbound in FacebookOutboundMessage.objects.filter(
        pk__in=due_ids
    ).select_related("account"):
        if send_outbound(outbound):
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed, "unconfirmed": stale}

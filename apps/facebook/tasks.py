"""
Celery tasks for Facebook Page webhooks (Messenger + Page comments).

Orchestration only: business logic lives in services/. Mirrors apps/instagram/tasks.py.
"""

import hashlib
import logging

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q

from apps.facebook.models.webhook import FacebookWebhookEventLog
from celery import shared_task

logger = logging.getLogger(__name__)

_MAX_EVENT_ATTEMPTS = 3


@shared_task(
    bind=True,
    max_retries=_MAX_EVENT_ATTEMPTS,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_facebook_event(self, event_id: int):
    try:
        event = FacebookWebhookEventLog.objects.select_related("account").get(
            pk=event_id
        )
    except FacebookWebhookEventLog.DoesNotExist:
        logger.error("process_facebook_event: event %s not found", event_id)
        return

    if event.status == FacebookWebhookEventLog.Status.PROCESSED:
        return

    event.mark_processing()
    try:
        _dispatch(event)
        event.mark_processed()
    except Exception as exc:
        error_msg = str(exc)[:5000]
        logger.exception(
            "process_facebook_event: event %s failed: %s", event_id, error_msg
        )
        event.mark_failed(error_msg)
        raise self.retry(exc=exc, countdown=60) from exc


def _dispatch(event: FacebookWebhookEventLog) -> None:
    """Hand the event to its handler, if the business can use the Facebook inbox."""
    from apps.billing import api as billing_api
    from apps.facebook.models.page import TenantResolutionError, get_page_for_webhook
    from apps.facebook.services.inbound import handle_comment, handle_dm

    if event.account is None:
        logger.info("process_facebook_event: event %s has no business", event.pk)
        return
    try:
        conn = get_page_for_webhook(event.page_id)
    except TenantResolutionError:
        logger.info(
            "process_facebook_event: Page %s no longer connected", event.page_id
        )
        return
    if conn.account_id != event.account_id:
        return
    if not billing_api.usable(event.account, "facebook"):
        # The Page is connected for Content Studio publishing only.
        logger.debug(
            "process_facebook_event: account %s has no Facebook inbox", event.account_id
        )
        return

    if event.event_type == FacebookWebhookEventLog.EventType.MESSAGE:
        handle_dm(event, conn)
    elif event.event_type == FacebookWebhookEventLog.EventType.COMMENT:
        handle_comment(event, conn)


@shared_task(acks_late=True, reject_on_worker_lost=True)
def drain_facebook_outbox() -> dict:
    """Retry Facebook sends that failed transiently (their backoff has elapsed)."""
    from apps.facebook.services.outbound import drain_outbox

    return drain_outbox()


_MEDIA_MAX_ATTEMPTS = 5
_MEDIA_BATCH = 20


@shared_task
def download_facebook_media(facebook_message_id: int | None = None) -> dict:
    """Copy inbound Messenger attachments into storage before Meta's CDN links expire."""
    from apps.facebook.models import FacebookMessage
    from apps.instagram.tasks import _ext_for_mime, _fetch_media

    pending = FacebookMessage.objects.filter(
        direction=FacebookMessage.Direction.INBOUND,
        media_attempts__lt=_MEDIA_MAX_ATTEMPTS,
    ).exclude(media_source_url="")
    pending = pending.filter(Q(media_file="") | Q(media_file__isnull=True))
    if facebook_message_id is not None:
        pending = pending.filter(pk=facebook_message_id)

    downloaded = failed = 0
    for msg in pending.select_related("conversation")[:_MEDIA_BATCH]:
        try:
            if msg.conversation is None:
                raise RuntimeError("Media message has no Messenger conversation.")
            content, mime = _fetch_media(msg.media_source_url)
            digest = hashlib.sha256(msg.message_id.encode()).hexdigest()[:24]
            name = default_storage.save(
                f"facebook/{msg.conversation.account_id}/{digest}{_ext_for_mime(mime)}",
                ContentFile(content),
            )
            msg.media_file = name
            msg.media_mime_type = mime
            msg.media_size = len(content)
            msg.media_error = ""
            msg.media_attempts += 1
            msg.save(
                update_fields=[
                    "media_file",
                    "media_mime_type",
                    "media_size",
                    "media_error",
                    "media_attempts",
                ]
            )
            downloaded += 1
        except Exception as exc:
            msg.media_attempts += 1
            msg.media_error = str(exc)[:500]
            msg.save(update_fields=["media_attempts", "media_error"])
            logger.warning(
                "download_facebook_media: message %s failed: %s", msg.pk, exc
            )
            failed += 1
    return {"downloaded": downloaded, "failed": failed}


@shared_task
def delete_facebook_page_data(account_id: int, page_id: str, confirmation_code: str):
    """Fulfil a Meta data-deletion request: everything received through this Page.

    Deletes the Page's Messenger threads, comments, outbox rows and stored webhook
    payloads, the inbox conversations they backed, stored attachments, and the
    Facebook identities (plus the canonical contact Facebook created) of people who
    reached the business only through this Page.
    """
    from apps.contacts.models import Contact
    from apps.conversations.models import ChannelConversation, Conversation
    from apps.facebook.models import (
        FacebookCommentThread,
        FacebookContact,
        FacebookConversation,
        FacebookMessage,
        FacebookOutboundMessage,
    )

    with transaction.atomic():
        convos = FacebookConversation.objects.filter(
            account_id=account_id, page_id=page_id
        )
        threads = FacebookCommentThread.objects.filter(
            account_id=account_id, page_id=page_id
        )
        contact_ids = set(convos.values_list("facebook_contact_id", flat=True)) | set(
            threads.values_list("facebook_contact_id", flat=True)
        )
        media_paths = [
            name
            for name in FacebookMessage.objects.filter(conversation__in=convos)
            .exclude(media_file="")
            .exclude(media_file__isnull=True)
            .values_list("media_file", flat=True)
            if name
        ] + list(
            FacebookOutboundMessage.objects.filter(
                account_id=account_id, page_id=page_id
            )
            .exclude(media_path="")
            .values_list("media_path", flat=True)
        )
        spine_ids = list(
            ChannelConversation.objects.filter(
                channel=Conversation.Channel.FACEBOOK,
                object_id__in=list(convos.values_list("pk", flat=True)),
            ).values_list("conversation_id", flat=True)
        )
        Conversation.objects.filter(pk__in=spine_ids).delete()
        convos.delete()
        threads.delete()
        FacebookOutboundMessage.objects.filter(
            account_id=account_id, page_id=page_id
        ).delete()
        FacebookWebhookEventLog.objects.filter(
            account_id=account_id, page_id=page_id
        ).delete()

        orphaned = (
            FacebookContact.objects.filter(pk__in=contact_ids)
            .exclude(conversations__isnull=False)
            .exclude(comment_threads__isnull=False)
        )
        canonical = set(
            orphaned.exclude(contact__isnull=True).values_list("contact_id", flat=True)
        )
        orphaned.delete()
        Contact.objects.filter(
            pk__in=canonical,
            source="facebook",
            facebook_contacts__isnull=True,
            instagram_contacts__isnull=True,
            whatsapp_contacts__isnull=True,
            orders__isnull=True,
            deals__isnull=True,
        ).delete()

    for path in media_paths:
        try:
            default_storage.delete(path)
        except Exception:
            logger.warning("delete_facebook_page_data: could not delete media %s", path)
    logger.info(
        "delete_facebook_page_data: account=%s page=%s code=%s",
        account_id,
        page_id,
        confirmation_code,
    )

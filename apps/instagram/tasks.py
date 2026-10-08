"""
Celery tasks for Instagram webhook processing.

The task is an orchestrator only — it delegates all business logic to
services/inbound.py. No Meta API details live here.
"""

import hashlib
import logging
import mimetypes
from datetime import timedelta

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.instagram.models.webhook import WebhookEventLog
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
def process_instagram_event(self, event_id: int):
    try:
        event = WebhookEventLog.objects.select_related(
            "instagram_account__account"
        ).get(pk=event_id)
    except WebhookEventLog.DoesNotExist:
        logger.error("process_instagram_event: event %s not found", event_id)
        return

    if event.status == WebhookEventLog.Status.PROCESSED:
        logger.debug("process_instagram_event: event %s already processed", event_id)
        return

    event.mark_processing()

    try:
        _dispatch(event)
        event.mark_processed()
    except Exception as exc:
        error_msg = str(exc)[:5000]
        logger.exception(
            "process_instagram_event: event %s failed: %s", event_id, error_msg
        )
        event.mark_failed(error_msg)
        raise self.retry(exc=exc, countdown=60) from exc


@shared_task(acks_late=True, reject_on_worker_lost=True)
def drain_instagram_outbox() -> dict:
    """Retry Instagram sends that failed transiently (their backoff has elapsed)."""
    from apps.instagram.services.outbound import drain_outbox

    return drain_outbox()


# Refresh a long-lived token this long before it expires (Meta: 60-day tokens,
# refreshable once at least 24 hours old).
_REFRESH_AHEAD = timedelta(days=10)


@shared_task
def refresh_instagram_tokens() -> dict:
    """Keep every connected Instagram account's token alive.

    Without this every connection silently dies 60 days after it was made. A
    refresh Meta refuses means the token is already dead: the account is marked
    ``token_expired`` so the business sees a Reconnect prompt.
    """
    from django.db.models import Q

    from apps.instagram.models.account import InstagramBusinessAccount
    from apps.instagram.oauth import InstagramOAuthError, refresh_long_lived_token

    now = timezone.now()
    due = InstagramBusinessAccount.objects.filter(
        Q(token_expires_at__lte=now + _REFRESH_AHEAD)
        # Connected before expiry was recorded: refresh once to learn it. Meta only
        # refreshes a token at least 24 hours old, so a just-saved one waits a day.
        | Q(token_expires_at__isnull=True, updated_at__lte=now - timedelta(days=1)),
        is_active=True,
        token_expired=False,
    )
    refreshed = failed = 0
    for iba in due:
        if not iba.access_token:
            continue
        if iba.token_expires_at is None and not iba.access_token.startswith("IG"):
            # No expiry and not an Instagram Login token (IGAA…): a permanent
            # system-user/Page token an operator entered — nothing to refresh.
            continue
        try:
            token, expires_in = refresh_long_lived_token(iba.access_token)
        except InstagramOAuthError as exc:
            logger.warning(
                "refresh_instagram_tokens: account %s refresh refused: %s", iba.pk, exc
            )
            iba.token_expired = True
            iba.save(update_fields=["token_expired", "updated_at"])
            failed += 1
            continue
        except Exception:
            # Network trouble: try again tomorrow, there are days of margin.
            logger.exception("refresh_instagram_tokens: account %s errored", iba.pk)
            failed += 1
            continue
        iba.access_token = token
        iba.token_expires_at = now + timedelta(seconds=expires_in or 60 * 24 * 3600)
        iba.save(update_fields=["access_token", "token_expires_at", "updated_at"])
        refreshed += 1
    return {"refreshed": refreshed, "failed": failed}


@shared_task
def delete_instagram_account_data(instagram_account_id: int, confirmation_code: str):
    """Fulfil a Meta data-deletion request for one connected Instagram account.

    Deletes the account row, which cascades to its Instagram contacts' threads,
    messages, comments and outbox, plus the inbox conversations those threads
    backed (they hold the same messages).
    """
    from apps.conversations.models import ChannelConversation, Conversation
    from apps.instagram.models import InstagramBusinessAccount, InstagramConversation

    iba = InstagramBusinessAccount.objects.filter(pk=instagram_account_id).first()
    if iba is None:
        return
    with transaction.atomic():
        thread_ids = list(
            InstagramConversation.objects.filter(instagram_account=iba).values_list(
                "pk", flat=True
            )
        )
        spine_ids = ChannelConversation.objects.filter(
            channel=Conversation.Channel.INSTAGRAM, object_id__in=thread_ids
        ).values_list("conversation_id", flat=True)
        Conversation.objects.filter(pk__in=list(spine_ids)).delete()
        iba.delete()
    logger.info(
        "delete_instagram_account_data: deleted instagram_account=%s code=%s",
        instagram_account_id,
        confirmation_code,
    )


_MEDIA_MAX_ATTEMPTS = 5
_MEDIA_BATCH = 20


@shared_task
def download_instagram_media(instagram_message_id: int | None = None) -> dict:
    """Copy inbound Instagram attachments (photos, videos, voice notes) into storage.

    Meta's attachment URLs are short-lived CDN links, so a new message is fetched
    straight away (``instagram_message_id``); the periodic run (no argument) sweeps
    up anything that failed. A row that keeps failing is retired after
    ``_MEDIA_MAX_ATTEMPTS`` so it stops being re-selected.
    """
    from apps.instagram.models import InstagramMessage

    pending = InstagramMessage.objects.filter(
        direction=InstagramMessage.Direction.INBOUND,
        media_attempts__lt=_MEDIA_MAX_ATTEMPTS,
    ).exclude(media_source_url="")
    pending = pending.filter(Q(media_file="") | Q(media_file__isnull=True))
    if instagram_message_id is not None:
        pending = pending.filter(pk=instagram_message_id)

    downloaded = failed = 0
    for msg in pending.select_related("conversation__instagram_account")[:_MEDIA_BATCH]:
        try:
            content, mime = _fetch_media(msg.media_source_url)
            ext = _ext_for_mime(mime)
            if msg.conversation is None:
                raise RuntimeError("Media message has no DM conversation.")
            account_id = msg.conversation.instagram_account.account_id
            digest = hashlib.sha256(msg.message_id.encode()).hexdigest()[:24]
            name = default_storage.save(
                f"instagram/{account_id}/{digest}{ext}", ContentFile(content)
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
                "download_instagram_media: failed for message %s (attempt %s): %s",
                msg.pk,
                msg.media_attempts,
                exc,
            )
            failed += 1
    return {"downloaded": downloaded, "failed": failed}


def _fetch_media(url: str) -> tuple[bytes, str]:
    """Download one attachment, refusing anything over the media size limit."""
    limit = settings.WHATSAPP_MAX_MEDIA_BYTES
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        declared = int(resp.headers.get("Content-Length") or 0)
        if declared > limit:
            raise RuntimeError(f"Media {declared}B exceeds the {limit}B limit")
        chunks, size = [], 0
        for chunk in resp.iter_content(64 * 1024):
            size += len(chunk)
            if size > limit:
                raise RuntimeError(f"Media exceeds the {limit}B limit")
            chunks.append(chunk)
        mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
    return b"".join(chunks), mime or "application/octet-stream"


def _ext_for_mime(mime: str) -> str:
    return mimetypes.guess_extension(mime) or ".bin"


def _dispatch(event: WebhookEventLog) -> None:
    from apps.instagram.services.inbound import handle_comment, handle_dm

    if event.event_type == WebhookEventLog.EventType.MESSAGE:
        handle_dm(event)
    elif event.event_type == WebhookEventLog.EventType.COMMENT:
        handle_comment(event)
    else:
        logger.debug(
            "process_instagram_event: unhandled event type %s for event %s",
            event.event_type,
            event.pk,
        )

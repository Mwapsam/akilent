"""
Celery tasks for Instagram webhook processing.

The task is an orchestrator only — it delegates all business logic to
services/inbound.py. No Meta API details live here.
"""

import logging
from datetime import timedelta

from django.db import transaction
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
    from apps.instagram.models.account import InstagramBusinessAccount
    from apps.instagram.oauth import InstagramOAuthError, refresh_long_lived_token

    now = timezone.now()
    due = InstagramBusinessAccount.objects.filter(
        is_active=True,
        token_expired=False,
        token_expires_at__isnull=False,
        token_expires_at__lte=now + _REFRESH_AHEAD,
    )
    refreshed = failed = 0
    for iba in due:
        if not iba.access_token:
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

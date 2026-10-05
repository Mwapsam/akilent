"""
Celery tasks for Instagram webhook processing.

The task is an orchestrator only — it delegates all business logic to
services/inbound.py. No Meta API details live here.
"""

import logging

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

"""Replay of inbound webhook events that arrived while their channel was switched off.

A webhook for a channel that is off is validated, stored (WhatsApp: ``held=True``; Instagram:
``status=held``) and acknowledged with 200, so nothing is lost. Nothing is processed until the
channel is back on. ``replay_held_webhooks`` releases the stored events, oldest first.

Each event is claimed with a conditional update, so two replays can't release the same event.
Processing is idempotent (WhatsApp by message id, Instagram by event id), so a replayed event
can't create a second message, conversation or status change. If the enqueue fails, the event
goes back on hold rather than being lost.
"""

import logging

from apps.core.channels import channel_state, invalidate_channel_cache
from celery import shared_task

logger = logging.getLogger(__name__)

_BATCH = 200
_MAX_BATCHES = 500  # 100k events per run at most; the sweep continues from there


@shared_task(name="apps.core.webhook_replay.replay_held_webhooks")
def replay_held_webhooks() -> dict:
    # This worker may have read the switch up to a cache TTL ago; read it fresh.
    invalidate_channel_cache()
    result = {}
    if channel_state("whatsapp") == "enabled":
        result["whatsapp"] = _drain(_replay_whatsapp)
    if channel_state("instagram") == "enabled":
        result["instagram"] = _drain(_replay_instagram)
    return result


def _drain(replay_batch) -> int:
    """Release held events until none are left (bounded), so the backlog clears in one run."""
    total = 0
    for _ in range(_MAX_BATCHES):
        released = replay_batch()
        total += released
        if released < _BATCH:
            break
    return total


def _replay_whatsapp() -> int:
    from apps.whatsapp.models import WebhookEventLog
    from apps.whatsapp.tasks import process_whatsapp_event

    ids = list(
        WebhookEventLog.objects.filter(
            source=WebhookEventLog.Source.WHATSAPP, held=True
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)[:_BATCH]
    )
    released = 0
    for pk in ids:
        if not WebhookEventLog.objects.filter(pk=pk, held=True).update(held=False):
            continue  # another replay released it first
        released += 1
        try:
            process_whatsapp_event.delay(pk)
        except Exception:
            WebhookEventLog.objects.filter(pk=pk).update(held=True)
            logger.exception("replay: could not enqueue WhatsApp webhook %s", pk)
            break  # broker is down; keep the rest held, in order
    return released


def _replay_instagram() -> int:
    from apps.instagram.models.webhook import WebhookEventLog
    from apps.instagram.tasks import process_instagram_event

    held = WebhookEventLog.Status.HELD
    received = WebhookEventLog.Status.RECEIVED
    ids = list(
        WebhookEventLog.objects.filter(status=held)
        .order_by("received_at", "id")
        .values_list("id", flat=True)[:_BATCH]
    )
    released = 0
    for pk in ids:
        if not WebhookEventLog.objects.filter(pk=pk, status=held).update(
            status=received
        ):
            continue
        released += 1
        try:
            process_instagram_event.delay(pk)
        except Exception:
            WebhookEventLog.objects.filter(pk=pk).update(status=held)
            logger.exception("replay: could not enqueue Instagram webhook %s", pk)
            break
    return released


# ---------------------------------------------------------------------------
# Stuck events: stored and acknowledged, but never processed
# ---------------------------------------------------------------------------

# An event this old that still isn't processed was lost on the way to (or out of)
# the worker: an enqueue dropped while the broker was down, a killed process, or
# Celery's own retries running out on a transient error.
_STUCK_AFTER_MINUTES = 10
# Total processing attempts before an event is left for a person to look at.
_MAX_SWEEP_ATTEMPTS = 6


@shared_task(name="apps.core.webhook_replay.sweep_stuck_webhooks")
def sweep_stuck_webhooks() -> dict:
    """Re-enqueue webhook events that were stored but never processed.

    Bounded by each event's attempt count, so an event that keeps failing stops
    being retried. Also covers a WhatsApp webhook that arrived during Embedded
    Signup before its number was saved (tenant not found yet): it is retried once
    the number exists. Processing is idempotent, so a re-enqueue is always safe.
    """
    invalidate_channel_cache()
    result = {}
    if channel_state("whatsapp") == "enabled":
        result["whatsapp"] = _sweep_whatsapp()
    if channel_state("instagram") == "enabled":
        result["instagram"] = _sweep_instagram()
    return result


def _stuck_cutoff():
    from datetime import timedelta

    from django.utils import timezone

    return timezone.now() - timedelta(minutes=_STUCK_AFTER_MINUTES)


def _sweep_whatsapp() -> int:
    from apps.whatsapp.models import WebhookEventLog
    from apps.whatsapp.tasks import process_whatsapp_event

    ids = list(
        WebhookEventLog.objects.filter(
            source=WebhookEventLog.Source.WHATSAPP,
            processed=False,
            held=False,
            attempts__lt=_MAX_SWEEP_ATTEMPTS,
            created_at__lt=_stuck_cutoff(),
        )
        .order_by("created_at", "id")
        .values_list("id", flat=True)[:_BATCH]
    )
    return _enqueue_all(ids, process_whatsapp_event, "WhatsApp")


def _sweep_instagram() -> int:
    from apps.instagram.models.webhook import WebhookEventLog
    from apps.instagram.tasks import process_instagram_event

    Status = WebhookEventLog.Status
    ids = list(
        WebhookEventLog.objects.filter(
            status__in=[Status.RECEIVED, Status.PROCESSING, Status.FAILED],
            instagram_account__isnull=False,  # unresolved: no business to process it for
            attempts__lt=_MAX_SWEEP_ATTEMPTS,
            received_at__lt=_stuck_cutoff(),
        )
        .order_by("received_at", "id")
        .values_list("id", flat=True)[:_BATCH]
    )
    return _enqueue_all(ids, process_instagram_event, "Instagram")


def _enqueue_all(ids, task, label: str) -> int:
    swept = 0
    for pk in ids:
        try:
            task.delay(pk)
        except Exception:
            logger.exception("sweep: could not enqueue %s webhook %s", label, pk)
            break  # broker is down; the next sweep picks them up
        swept += 1
    if swept:
        logger.warning("sweep: re-enqueued %s stuck %s webhook events", swept, label)
    return swept


# ---------------------------------------------------------------------------
# Retention: raw webhook bodies hold customers' messages, names and numbers
# ---------------------------------------------------------------------------

# Meta redelivers a webhook for at most 7 days, so a processed event is only kept
# for diagnosis. One that never processed is kept longer, for someone to investigate.
PROCESSED_RETENTION_DAYS = 30
UNPROCESSED_RETENTION_DAYS = 90
_PRUNE_BATCH = 2000


@shared_task(name="apps.core.webhook_replay.prune_webhook_logs")
def prune_webhook_logs() -> dict:
    """Delete stored WhatsApp and Instagram webhook bodies past their retention.

    Held events are never pruned: they are waiting for their channel to come back on.
    Deletes in batches so one run can't hold a long lock on a large table.
    """
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    from apps.instagram.models.webhook import WebhookEventLog as IGEvent
    from apps.whatsapp.models import WebhookEventLog as WAEvent

    now = timezone.now()
    processed_before = now - timedelta(days=PROCESSED_RETENTION_DAYS)
    unprocessed_before = now - timedelta(days=UNPROCESSED_RETENTION_DAYS)

    wa = WAEvent.objects.filter(held=False).filter(
        Q(processed=True, created_at__lt=processed_before)
        | Q(created_at__lt=unprocessed_before)
    )
    ig = IGEvent.objects.exclude(status=IGEvent.Status.HELD).filter(
        Q(status=IGEvent.Status.PROCESSED, received_at__lt=processed_before)
        | Q(received_at__lt=unprocessed_before)
    )
    return {"whatsapp": _delete_in_batches(wa), "instagram": _delete_in_batches(ig)}


def _delete_in_batches(queryset) -> int:
    deleted = 0
    while True:
        ids = list(queryset.values_list("pk", flat=True)[:_PRUNE_BATCH])
        if not ids:
            return deleted
        queryset.model.objects.filter(pk__in=ids).delete()
        deleted += len(ids)

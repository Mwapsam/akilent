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

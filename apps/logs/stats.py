"""Incremental + reconcilable daily rollups from MessageEvent.

``apply_event`` is called synchronously from ``record_message_event``.
``reconcile`` recomputes a day straight from raw events and is the safety net
for any increment that was missed (crash between the event write and the
counter bump).
"""
from __future__ import annotations

import logging

from django.db.models import F

from apps.logs.models import MessageEvent, MessageStatsDaily

logger = logging.getLogger(__name__)

# MessageEvent.Type -> MessageStatsDaily measure field.
_MEASURE_FOR_TYPE = {
    MessageEvent.Type.SENT: "sent",
    MessageEvent.Type.DELIVERED: "delivered",
    MessageEvent.Type.BOUNCED: "bounced",
    MessageEvent.Type.COMPLAINED: "complained",
    MessageEvent.Type.REJECTED: "rejected",
    MessageEvent.Type.OPENED: "opened",
    MessageEvent.Type.CLICKED: "clicked",
    MessageEvent.Type.UNSUBSCRIBED: "unsubscribed",
}


def _dims(event: MessageEvent) -> dict:
    msg = event.message
    return {
        "account_id": event.account_id,
        "domain_id": msg.domain_id,
        "template_id": msg.template_id,
        "campaign_id": msg.campaign_id,
        "day": event.occurred_at.date(),
        "key_mode": getattr(msg, "key_mode", "") or "live",
    }


def apply_event(event: MessageEvent) -> None:
    measure = _MEASURE_FOR_TYPE.get(event.type)
    if not measure:
        return

    row, _ = MessageStatsDaily.objects.get_or_create(**_dims(event))
    updates = {measure: F(measure) + 1}

    if event.type == MessageEvent.Type.OPENED and _is_first(event, "opened"):
        updates["unique_opens"] = F("unique_opens") + 1
    elif event.type == MessageEvent.Type.CLICKED and _is_first(event, "clicked"):
        updates["unique_clicks"] = F("unique_clicks") + 1

    MessageStatsDaily.objects.filter(pk=row.pk).update(**updates)


def _is_first(event: MessageEvent, event_type: str) -> bool:
    return not (
        MessageEvent.objects.filter(message_id=event.message_id, type=event_type)
        .exclude(pk=event.pk)
        .exists()
    )


def reconcile(day) -> int:
    """Rebuild every MessageStatsDaily row for ``day`` from raw events."""
    from collections import defaultdict

    counters: dict[tuple, dict] = defaultdict(lambda: defaultdict(int))
    seen_open: set = set()
    seen_click: set = set()

    events = (
        MessageEvent.objects.filter(occurred_at__date=day)
        .select_related("message")
        .order_by("occurred_at", "id")
    )
    for event in events.iterator():
        measure = _MEASURE_FOR_TYPE.get(event.type)
        if not measure:
            continue
        msg = event.message
        key = (
            event.account_id,
            msg.domain_id,
            msg.template_id,
            msg.campaign_id,
            getattr(msg, "key_mode", "") or "live",
        )
        counters[key][measure] += 1
        if event.type == MessageEvent.Type.OPENED and event.message_id not in seen_open:
            seen_open.add(event.message_id)
            counters[key]["unique_opens"] += 1
        elif event.type == MessageEvent.Type.CLICKED and event.message_id not in seen_click:
            seen_click.add(event.message_id)
            counters[key]["unique_clicks"] += 1

    written = 0
    for key, measures in counters.items():
        account_id, domain_id, template_id, campaign_id, key_mode = key
        MessageStatsDaily.objects.update_or_create(
            account_id=account_id,
            domain_id=domain_id,
            template_id=template_id,
            campaign_id=campaign_id,
            day=day,
            key_mode=key_mode,
            defaults=measures,
        )
        written += 1
    return written

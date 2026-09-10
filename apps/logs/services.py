"""The single writer for :class:`apps.logs.models.MessageEvent`.

``record_message_event`` appends the event row, reconciles the denormalized
``EmailMessage.status`` and fans out to every downstream consumer. The fan-out
is best-effort: a failing consumer is logged and swallowed so it can never
break the send / tracking / webhook request path that produced the event.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.logs.models import MessageEvent

logger = logging.getLogger(__name__)

# MessageEvent.Type -> public webhook event name.
_WEBHOOK_EVENT_NAMES = {
    MessageEvent.Type.SENT: "message.sent",
    MessageEvent.Type.FAILED: "message.failed",
    MessageEvent.Type.DELIVERED: "message.delivered",
    MessageEvent.Type.BOUNCED: "message.bounced",
    MessageEvent.Type.COMPLAINED: "message.complained",
    MessageEvent.Type.REJECTED: "message.failed",
    MessageEvent.Type.OPENED: "message.opened",
    MessageEvent.Type.CLICKED: "message.clicked",
    MessageEvent.Type.UNSUBSCRIBED: "message.unsubscribed",
}

# Precedence for the denormalized EmailMessage.status. A higher rank never gets
# overwritten by a lower one, so a late "opened" can't undo a "bounced". Types
# absent from this map (accepted/queued-noise, rendered, deferred, opened,
# clicked, unsubscribed) leave status untouched.
_STATUS_FOR_TYPE = {
    MessageEvent.Type.QUEUED: "queued",
    MessageEvent.Type.SENT: "sent",
    MessageEvent.Type.DELIVERED: "delivered",
    MessageEvent.Type.FAILED: "failed",
    MessageEvent.Type.REJECTED: "failed",
    MessageEvent.Type.BOUNCED: "bounced",
    MessageEvent.Type.COMPLAINED: "complained",
}
_STATUS_RANK = {
    "queued": 1,
    "sent": 2,
    "delivered": 3,
    "failed": 4,
    "bounced": 4,
    "complained": 4,
}


def _event_name(event_type: str) -> str | None:
    return _WEBHOOK_EVENT_NAMES.get(event_type)


def record_message_event(
    message,
    event_type: str,
    *,
    source: str,
    occurred_at=None,
    data: dict | None = None,
    provider_event_id: str | None = None,
    request_id: str = "",
) -> MessageEvent | None:
    """Append a :class:`MessageEvent` and fan it out.

    Returns the created row, or ``None`` if it was suppressed as a duplicate of
    an already-recorded provider event.
    """
    if provider_event_id and MessageEvent.objects.filter(
        message=message, type=event_type, provider_event_id=provider_event_id
    ).exists():
        return None

    if not request_id:
        from apps.core.request_context import get_request_id

        request_id = get_request_id()

    event = MessageEvent.objects.create(
        message=message,
        account_id=message.account_id,
        type=event_type,
        source=source,
        occurred_at=occurred_at or timezone.now(),
        data=data or {},
        provider_event_id=provider_event_id or None,
        request_id=request_id or "",
    )

    _reconcile_status(message, event_type)
    _fan_out(event)
    return event


def _reconcile_status(message, event_type: str) -> None:
    new_status = _STATUS_FOR_TYPE.get(event_type)
    if not new_status:
        return
    current_rank = _STATUS_RANK.get(message.status, 0)
    if _STATUS_RANK[new_status] <= current_rank:
        return
    message.status = new_status
    message.save(update_fields=["status"])


def _fan_out(event: MessageEvent) -> None:
    for sink in (_sink_webhooks, _sink_analytics, _sink_contact_activity, _sink_workflows):
        try:
            sink(event)
        except Exception:  # noqa: BLE001 - a sink must never break the caller
            logger.exception(
                "record_message_event: sink %s failed",
                getattr(sink, "__name__", repr(sink)),
            )


def _sink_webhooks(event: MessageEvent) -> None:
    name = _event_name(event.type)
    if not name:
        return
    from apps.email.webhooks import enqueue_event

    msg = event.message
    enqueue_event(
        name,
        account=event.account,
        message=msg,
        data={
            "id": msg.pk,
            "from": msg.from_email,
            "to": msg.to_email,
            "subject": msg.subject,
            "status": msg.status,
            "event": event.type,
            **(event.data or {}),
        },
    )


def _sink_analytics(event: MessageEvent) -> None:
    from apps.logs.stats import apply_event

    apply_event(event)


_CONTACT_ACTIVITY_TYPES = {
    "delivered", "opened", "clicked", "bounced", "complained", "unsubscribed",
}


def _sink_contact_activity(event: MessageEvent) -> None:
    if event.type not in _CONTACT_ACTIVITY_TYPES:
        return
    from apps.contacts.models import Contact
    from apps.contacts.services import record_contact_event

    contact = Contact.objects.filter(
        account_id=event.account_id, email__iexact=event.message.to_email
    ).first()
    if contact is None:
        return
    record_contact_event(
        contact,
        f"email.{event.type}",
        occurred_at=event.occurred_at,
        data={"message_id": event.message.public_id, **(event.data or {})},
    )


def _sink_workflows(event: MessageEvent) -> None:
    # Phase 6 wires the apps.core.events dispatcher / automation engine here.
    return

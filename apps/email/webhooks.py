"""Outbound webhook fan-out and HMAC signing.

Event triggers (EmailMessage.mark_sent/mark_failed, the tracking views) call
enqueue_event(); apps.email.tasks.deliver_webhook does the actual signed HTTP
POST with retry/backoff. Signature format mirrors Stripe's:
``t=<unix>,v1=<hex hmac-sha256>`` over ``"<t>.<raw body>"``.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time

from django.db import transaction

from apps.email.models import WebhookDelivery, WebhookEndpoint

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Akilent-Signature"
EVENT_HEADER = "X-Akilent-Event"
_TOLERANCE_SECONDS = 300


def _hmac_hex(secret: str, timestamp: int, raw_body: bytes) -> str:
    signed_payload = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()


def build_signature_header(secret: str, raw_body: bytes, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    digest = _hmac_hex(secret, ts, raw_body)
    return f"t={ts},v1={digest}"


def verify_signature(
    secret: str, raw_body: bytes, signature_header: str, *, tolerance: int = _TOLERANCE_SECONDS
) -> bool:
    """Verify a received webhook's signature header. For customers' own servers."""
    try:
        parts = dict(part.split("=", 1) for part in signature_header.split(","))
        timestamp = int(parts["t"])
        received_v1 = parts["v1"]
    except (KeyError, ValueError):
        return False
    if abs(time.time() - timestamp) > tolerance:
        return False
    expected_v1 = _hmac_hex(secret, timestamp, raw_body)
    return hmac.compare_digest(expected_v1, received_v1)


def enqueue_event(event_type: str, *, account, message=None, data: dict | None = None) -> list[int]:
    """Fan out an event to every active endpoint subscribed to it.

    Best-effort: never raises — a webhook subsystem failure must not break
    the message-send / tracking-pixel request paths that trigger it. Returns
    the WebhookDelivery ids created.
    """
    from apps.email.tasks import deliver_webhook

    delivery_ids: list[int] = []
    try:
        endpoints = WebhookEndpoint.objects.filter(account=account, is_active=True)
        for endpoint in endpoints:
            if event_type not in (endpoint.event_types or []):
                continue
            delivery = WebhookDelivery.objects.create(
                endpoint=endpoint,
                event_type=event_type,
                message=message,
                payload={"event": event_type, "data": data or {}},
            )
            delivery_ids.append(delivery.pk)
            transaction.on_commit(
                lambda delivery_id=delivery.pk: deliver_webhook.delay(delivery_id)
            )
    except Exception:
        logger.exception("enqueue_event: failed to fan out event %s", event_type)
    return delivery_ids


def notify(account, event_type: str, data: dict | None = None) -> list[int]:
    """Fan out a non-message event (contact / workflow / campaign / business).

    Thin wrapper over ``enqueue_event`` for events with no associated
    EmailMessage. Best-effort; never raises.
    """
    return enqueue_event(event_type, account=account, message=None, data=data or {})

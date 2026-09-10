"""Ingest a business event: store it, link/append contact activity, publish."""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.core.events import BusinessEventReceived, dispatcher
from apps.events.models import BusinessEvent

logger = logging.getLogger(__name__)

_NAME_MAX = 128


def _resolve_contact(account, customer_ref: str):
    from apps.contacts.models import Contact
    from apps.contacts.services import upsert_contact

    ref = (customer_ref or "").strip()
    if not ref:
        return None
    if ref.startswith("con_"):
        return Contact.objects.filter(account=account, public_id=ref).first()
    if "@" in ref:
        contact, _ = upsert_contact(account, ref, source="event")
        return contact
    # Opaque external id — stash it as an attribute for later matching.
    return Contact.objects.filter(account=account, attributes__external_id=ref).first()


def ingest_event(
    account,
    *,
    name: str,
    customer: str = "",
    data: dict | None = None,
    occurred_at=None,
    source: str = "api",
) -> BusinessEvent:
    if not name or not isinstance(name, str):
        raise ValueError("event name is required")
    name = name.strip()[:_NAME_MAX]

    contact = _resolve_contact(account, customer)
    event = BusinessEvent.objects.create(
        account=account,
        name=name,
        contact=contact,
        customer_ref=(customer or "")[:255],
        data=data or {},
        source=source,
        occurred_at=occurred_at or timezone.now(),
    )

    if contact is not None:
        try:
            from apps.contacts.services import record_contact_event

            record_contact_event(
                contact, name, occurred_at=event.occurred_at, data=event.data
            )
        except Exception:
            logger.exception("ingest_event: contact activity append failed for %s", event.pk)

    try:
        from apps.email.webhooks import notify

        notify(account, "event.received", {
            "id": event.public_id,
            "event": name,
            "contact": contact.public_id if contact else None,
            "data": event.data,
        })
    except Exception:
        logger.exception("ingest_event: webhook notify failed for %s", event.pk)

    try:
        dispatcher.publish(
            BusinessEventReceived(
                account_id=account.id,
                event_id=event.public_id,
                name=name,
                contact_id=contact.id if contact else None,
                data=event.data,
                occurred_at=event.occurred_at,
            )
        )
    except Exception:
        logger.exception("ingest_event: dispatch failed for %s", event.pk)

    return event

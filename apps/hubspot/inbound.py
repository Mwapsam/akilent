"""HubSpot inbound webhook processing.

process_webhook_event(event) applies one HubSpotWebhookEvent to local data.
Idempotent: if the event is already processed, it's a no-op.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.hubspot.field_map import CONTACT_FIELD_MAP
from apps.hubspot.models import (
    HubSpotContactLink,
    HubSpotFieldState,
    HubSpotWebhookEvent,
    SyncConflict,
)
from apps.hubspot.normalise import normalise_value

logger = logging.getLogger(__name__)

# Reverse map: HubSpot contact property → local Contact attribute
_HS_TO_LOCAL: dict[str, str] = {v: k for k, v in CONTACT_FIELD_MAP.items()}


def process_webhook_event(event: HubSpotWebhookEvent) -> None:
    """Apply one inbound webhook event to local data. Idempotent."""
    with transaction.atomic():
        locked = (
            HubSpotWebhookEvent.objects.select_for_update().filter(pk=event.pk).first()
        )
        if locked is None or locked.processed:
            return

        if event.object_type == "contact":
            _apply_contact_event(locked)
        # Deals and other object types can be added here.

        locked.processed = True
        locked.save(update_fields=["processed"])


def _apply_contact_event(event: HubSpotWebhookEvent) -> None:
    try:
        link = HubSpotContactLink.objects.select_for_update().get(
            account=event.account, hs_object_id=event.object_id
        )
    except HubSpotContactLink.DoesNotExist:
        # Unlinked remote object — fetch full object from HubSpot and create locally.
        link = _fetch_and_create_local_contact(event)
        if link is None:
            return

    if link.remote_archived:
        return

    hs_field = event.property_name
    local_field = _HS_TO_LOCAL.get(hs_field)
    if not local_field:
        return  # Field not in our map; ignore.

    remote_value = normalise_value(local_field, event.property_value)
    current_hash = HubSpotFieldState.hash_value(remote_value)

    state, _ = HubSpotFieldState.objects.select_for_update().get_or_create(
        contact_link=link,
        field=local_field,
        defaults={"direction": HubSpotFieldState.Direction.BIDIRECTIONAL},
    )

    # Echo check: remote value equals last synced value → skip.
    if state.synced_value_hash and state.synced_value_hash == current_hash:
        return

    # Ownership / conflict resolution.
    if state.direction == HubSpotFieldState.Direction.AKILENT:
        # Akilent-owned: remote change is silently ignored (push-only field).
        return

    if state.direction == HubSpotFieldState.Direction.HUBSPOT:
        _apply_contact_field(link.contact, local_field, remote_value, state, event)
        return

    # Bidirectional: compare source timestamps.
    remote_ts = event.occurred_at
    local_ts = state.synced_source_ts

    if local_ts is None or remote_ts > local_ts:
        # Remote wins.
        _apply_contact_field(link.contact, local_field, remote_value, state, event)
    elif remote_ts == local_ts:
        # Equal timestamps: keep Akilent value, write conflict.
        _record_conflict(
            link, local_field, state, remote_value, remote_ts, event.account
        )
    # else: local is newer → remote is stale → ignore (no conflict needed).


def _apply_contact_field(
    contact,
    local_field: str,
    value: str,
    state: HubSpotFieldState,
    event: HubSpotWebhookEvent,
) -> None:
    try:
        _write_contact_field(contact, local_field, value)
    except Exception as exc:
        logger.warning(
            "apply_contact_field failed for contact %s field %s: %s",
            contact.pk,
            local_field,
            exc,
        )
        return

    state.synced_value_hash = HubSpotFieldState.hash_value(value)
    state.synced_source_ts = event.occurred_at
    state.synced_at = timezone.now()
    state.save(update_fields=["synced_value_hash", "synced_source_ts", "synced_at"])


def _record_conflict(
    link: HubSpotContactLink,
    field: str,
    state: HubSpotFieldState,
    remote_value: str,
    remote_ts,
    account,
) -> None:
    local_value = normalise_value(field, str(getattr(link.contact, field, "") or ""))
    SyncConflict.objects.get_or_create(
        account=account,
        contact_link=link,
        field=field,
        status=SyncConflict.Status.OPEN,
        defaults=dict(
            local_value=local_value,
            remote_value=remote_value,
            local_source_ts=state.synced_source_ts,
            remote_source_ts=remote_ts,
        ),
    )


def _fetch_and_create_local_contact(event: HubSpotWebhookEvent):
    """Fetch the full HubSpot contact object and create an Akilent Contact."""
    from apps.hubspot.client import HubSpotAPIError, get_fresh_client
    from apps.hubspot.create import create_local_contact_from_hs
    from apps.hubspot.field_map import CONTACT_FIELD_MAP

    try:
        connection = event.account.hubspot_connection
    except Exception:
        return None

    try:
        client = get_fresh_client(connection)
        properties = ",".join(CONTACT_FIELD_MAP.values()) + ",email"
        data = client.get(
            f"/crm/v3/objects/contacts/{event.object_id}",
            properties=properties,
        )
    except HubSpotAPIError as exc:
        logger.warning(
            "inbound: could not fetch hs contact %s for auto-create: %s",
            event.object_id,
            exc,
        )
        return None

    link = create_local_contact_from_hs(event.account, data)
    if link:
        logger.info(
            "inbound: auto-created local contact %s for hs %s",
            link.contact_id,
            event.object_id,
        )
    return link


# Model fields that HubSpot maps to Contact model columns directly.
_CONTACT_MODEL_FIELDS = {"email", "phone", "first_name", "last_name"}


def _write_contact_field(contact, local_field: str, value: str) -> None:
    """Write a single field to the Contact model (direct column, not custom attr)."""
    if local_field not in _CONTACT_MODEL_FIELDS:
        return
    setattr(contact, local_field, value or None)
    contact.save(update_fields=[local_field, "updated_at"])

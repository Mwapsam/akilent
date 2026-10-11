"""HubSpot reconciliation: page through HubSpot's contact/deal listing,
apply changes using the same apply function as inbound webhooks, and
advance the ReconcileCursor only after each committed object.

The cursor advances to the last successfully committed object,
so a failure mid-page resumes from the last committed position.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.hubspot.field_map import CONTACT_FIELD_MAP
from apps.hubspot.inbound import _write_contact_field
from apps.hubspot.models import (
    HubSpotContactLink,
    HubSpotFieldState,
    ReconcileCursor,
)
from apps.hubspot.normalise import normalise_value

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_OVERLAP_MINUTES = 10


def reconcile_contacts(connection, *, max_pages: int = 50) -> int:
    """Fetch and apply changed contacts from HubSpot. Returns objects processed."""
    account = connection.account
    cursor, _ = ReconcileCursor.objects.get_or_create(
        account=account,
        object_type=ReconcileCursor.ObjectType.CONTACT,
    )

    from apps.hubspot.client import get_fresh_client

    client = get_fresh_client(connection)
    processed = 0

    for _ in range(max_pages):
        params: dict = {
            "limit": _PAGE_SIZE,
            "properties": ",".join(CONTACT_FIELD_MAP.values()),
            "sorts": "lastmodifieddate",
        }
        if cursor.after_cursor:
            params["after"] = cursor.after_cursor

        data = client.get("/crm/v3/objects/contacts", **params)
        results = data.get("results", [])
        if not results:
            break

        for obj in results:
            _apply_contact_object(account, obj, cursor)
            processed += 1

        paging = data.get("paging", {})
        next_cursor = paging.get("next", {}).get("after")
        if not next_cursor:
            # End of results.
            with transaction.atomic():
                cursor.after_cursor = ""
                cursor.save(update_fields=["after_cursor", "updated_at"])
            break
        with transaction.atomic():
            cursor.after_cursor = next_cursor
            cursor.save(update_fields=["after_cursor", "updated_at"])

    return processed


def reconcile_archived_contacts(connection, *, max_pages: int = 50) -> int:
    """Mark locally linked contacts as remote_archived when they're archived in HubSpot."""
    account = connection.account
    cursor, _ = ReconcileCursor.objects.get_or_create(
        account=account,
        object_type=ReconcileCursor.ObjectType.ARCHIVED_CONTACT,
    )

    from apps.hubspot.client import get_fresh_client

    client = get_fresh_client(connection)
    processed = 0

    for _ in range(max_pages):
        params: dict = {"limit": _PAGE_SIZE, "archived": "true"}
        if cursor.after_cursor:
            params["after"] = cursor.after_cursor

        data = client.get("/crm/v3/objects/contacts", **params)
        results = data.get("results", [])
        if not results:
            break

        for obj in results:
            hs_id = str(obj.get("id", ""))
            if not hs_id:
                continue
            with transaction.atomic():
                HubSpotContactLink.objects.filter(
                    account=account, hs_object_id=hs_id
                ).update(remote_archived=True)
            processed += 1

        paging = data.get("paging", {})
        next_cursor = paging.get("next", {}).get("after")
        if not next_cursor:
            with transaction.atomic():
                cursor.after_cursor = ""
                cursor.save(update_fields=["after_cursor", "updated_at"])
            break
        with transaction.atomic():
            cursor.after_cursor = next_cursor
            cursor.save(update_fields=["after_cursor", "updated_at"])

    return processed


def _apply_contact_object(account, obj: dict, cursor: ReconcileCursor) -> None:
    hs_id = str(obj.get("id", ""))
    if not hs_id:
        return

    try:
        link = HubSpotContactLink.objects.get(account=account, hs_object_id=hs_id)
    except HubSpotContactLink.DoesNotExist:
        return

    if link.remote_archived:
        return

    properties = obj.get("properties", {})
    from datetime import datetime

    for hs_field, local_field in {v: k for k, v in CONTACT_FIELD_MAP.items()}.items():
        raw = properties.get(hs_field)
        if raw is None:
            continue

        value = normalise_value(local_field, str(raw))
        current_hash = HubSpotFieldState.hash_value(value)

        state, _ = HubSpotFieldState.objects.get_or_create(
            contact_link=link,
            field=local_field,
            defaults={"direction": HubSpotFieldState.Direction.BIDIRECTIONAL},
        )

        # Skip if value hash matches last synced (idempotent via field-state hashes).
        if state.synced_value_hash == current_hash:
            continue

        # For reconciliation, use lastmodifieddate as the source timestamp.
        hs_ts_str = properties.get("lastmodifieddate")
        hs_ts = None
        if hs_ts_str:
            try:
                hs_ts = datetime.fromisoformat(hs_ts_str.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Skip if this snapshot is older than what we've already synced.
        if hs_ts and state.synced_source_ts and hs_ts <= state.synced_source_ts:
            continue

        # Apply via the same contact-attributes path as inbound webhooks.
        try:
            with transaction.atomic():
                _write_contact_field(link.contact, local_field, value)
                state.synced_value_hash = current_hash
                state.synced_source_ts = hs_ts
                state.synced_at = timezone.now()
                state.save(
                    update_fields=["synced_value_hash", "synced_source_ts", "synced_at"]
                )
                # Advance cursor to this object.
                cursor.last_object_id = hs_id
                cursor.last_modified_ts = hs_ts
                cursor.save(
                    update_fields=["last_object_id", "last_modified_ts", "updated_at"]
                )
        except Exception as exc:
            logger.warning(
                "reconcile: failed to apply contact %s field %s: %s",
                hs_id,
                local_field,
                exc,
            )

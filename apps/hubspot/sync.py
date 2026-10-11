"""HubSpot outbound sync service.

process_sync_job(job) pushes changed fields from Akilent to HubSpot.
enqueue_contact_sync(contact_link) creates or coalesces a pending job.
push_lead_status(lead) pushes the lead's hs_lead_status to HubSpot.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.hubspot.client import HubSpotAPIError, get_fresh_client
from apps.hubspot.field_map import (
    CONTACT_FIELD_MAP,
    HS_LEAD_STATUS_PROPERTY,
    LEAD_STATUS_MAP,
)
from apps.hubspot.models import (
    HubSpotContactLink,
    HubSpotDealLink,
    HubSpotFieldState,
    HubSpotSyncJob,
)
from apps.hubspot.normalise import normalise_value

logger = logging.getLogger(__name__)


def enqueue_contact_sync(contact_link: HubSpotContactLink) -> None:
    """Create or coalesce a pending sync job for this contact link."""
    _enqueue_job(contact_link.account, "contact", contact_link=contact_link)


def enqueue_deal_sync(deal_link: HubSpotDealLink) -> None:
    """Create or coalesce a pending sync job for this deal link."""
    _enqueue_job(deal_link.account, "deal", deal_link=deal_link)


def _enqueue_job(account, object_type: str, **link_kwargs) -> None:
    """Create a PENDING job, or reset an existing PENDING one (coalesce)."""
    with transaction.atomic():
        existing = (
            HubSpotSyncJob.objects.select_for_update(skip_locked=True)
            .filter(
                account=account,
                object_type=object_type,
                status=HubSpotSyncJob.Status.PENDING,
                **link_kwargs,
            )
            .first()
        )
        if existing:
            existing.updated_at = timezone.now()
            existing.save(update_fields=["updated_at"])
        else:
            HubSpotSyncJob.objects.create(
                account=account, object_type=object_type, **link_kwargs
            )


def process_sync_job(job: HubSpotSyncJob) -> None:
    """Push changed fields for one sync job to HubSpot.

    On success: updates field states and marks job DONE.
    On failure: increments attempts, sets retry_after, marks FAILED after max.
    """
    try:
        connection = job.account.hubspot_connection
    except Exception:
        job.status = HubSpotSyncJob.Status.FAILED
        job.last_error = "No HubSpot connection"
        job.save(update_fields=["status", "last_error", "updated_at"])
        return

    if connection.status != connection.Status.ACTIVE:
        return

    try:
        client = get_fresh_client(connection)
    except HubSpotAPIError as exc:
        _fail_job(job, str(exc))
        return

    if job.object_type == HubSpotSyncJob.ObjectType.CONTACT:
        _push_contact(client, job)
    elif job.object_type == HubSpotSyncJob.ObjectType.DEAL:
        _push_deal(client, job)


def _push_contact(client, job: HubSpotSyncJob) -> None:
    link = job.contact_link
    if not link or link.remote_archived:
        job.status = HubSpotSyncJob.Status.DONE
        job.save(update_fields=["status", "updated_at"])
        return

    contact = link.contact
    properties: dict[str, str] = {}

    for local_field, hs_field in CONTACT_FIELD_MAP.items():
        raw = getattr(contact, local_field, None)
        if raw is None:
            continue
        value = normalise_value(local_field, str(raw))
        state = HubSpotFieldState.objects.filter(
            contact_link=link, field=local_field
        ).first()
        current_hash = HubSpotFieldState.hash_value(value)
        if state and state.synced_value_hash == current_hash:
            continue
        properties[hs_field] = value

    if not properties:
        job.status = HubSpotSyncJob.Status.DONE
        job.save(update_fields=["status", "updated_at"])
        return

    payload = {"properties": properties}
    try:
        client.patch(f"/crm/v3/objects/contacts/{link.hs_object_id}", json=payload)
    except HubSpotAPIError as exc:
        _fail_job(job, str(exc))
        return

    now = timezone.now()
    with transaction.atomic():
        for local_field, hs_field in CONTACT_FIELD_MAP.items():
            if hs_field not in properties:
                continue
            value = properties[hs_field]
            HubSpotFieldState.objects.update_or_create(
                contact_link=link,
                field=local_field,
                defaults={
                    "synced_value_hash": HubSpotFieldState.hash_value(value),
                    "synced_at": now,
                    "direction": HubSpotFieldState.Direction.BIDIRECTIONAL,
                },
            )
        job.status = HubSpotSyncJob.Status.DONE
        job.sent_payload = payload
        job.save(update_fields=["status", "sent_payload", "updated_at"])


def _push_deal(client, job: HubSpotSyncJob) -> None:
    link = job.deal_link
    if not link or link.remote_archived:
        job.status = HubSpotSyncJob.Status.DONE
        job.save(update_fields=["status", "updated_at"])
        return

    deal = link.deal
    properties = {}
    if deal.title:
        properties["dealname"] = deal.title
    if deal.value is not None:
        properties["amount"] = str(deal.value)

    if not properties:
        job.status = HubSpotSyncJob.Status.DONE
        job.save(update_fields=["status", "updated_at"])
        return

    payload = {"properties": properties}
    try:
        client.patch(f"/crm/v3/objects/deals/{link.hs_object_id}", json=payload)
    except HubSpotAPIError as exc:
        _fail_job(job, str(exc))
        return

    with transaction.atomic():
        job.status = HubSpotSyncJob.Status.DONE
        job.sent_payload = payload
        job.save(update_fields=["status", "sent_payload", "updated_at"])


def _fail_job(job: HubSpotSyncJob, error: str, max_attempts: int = 5) -> None:
    from datetime import timedelta

    job.attempts += 1
    job.last_error = error
    if job.attempts >= max_attempts:
        job.status = HubSpotSyncJob.Status.FAILED
        job.retry_after = None
    else:
        # Exponential back-off: 30s, 2m, 8m, 30m
        delay = 30 * (4 ** (job.attempts - 1))
        job.retry_after = timezone.now() + timedelta(seconds=delay)
    job.save(
        update_fields=["attempts", "last_error", "status", "retry_after", "updated_at"]
    )


def push_lead_status(lead) -> None:
    """Push the lead's current status to hs_lead_status on its HubSpot Contact.

    Lead status is akilent-owned (push only). Silently skips if there is no
    HubSpot connection or no contact link.
    """
    try:
        connection = lead.account.hubspot_connection
    except Exception:
        return
    if connection.status != connection.Status.ACTIVE:
        return

    try:
        contact_link = lead.contact.hubspot_link
    except Exception:
        return
    if contact_link.remote_archived:
        return

    hs_status = LEAD_STATUS_MAP.get(lead.status)
    if not hs_status:
        return

    try:
        client = get_fresh_client(connection)
        client.patch(
            f"/crm/v3/objects/contacts/{contact_link.hs_object_id}",
            json={"properties": {HS_LEAD_STATUS_PROPERTY: hs_status}},
        )
    except HubSpotAPIError as exc:
        logger.warning("push_lead_status failed for lead %s: %s", lead.pk, exc)

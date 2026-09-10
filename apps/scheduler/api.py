"""The only entry point other apps import from apps.scheduler.

``should_schedule`` / ``resolve_fire_at`` are called from
apps.api.services.create_and_queue_* to decide "now vs later" and to turn the
API's ``scheduled_at`` + ``timezone`` into a UTC instant. The ``schedule_*``
functions create the ScheduledJob row; the drainer (apps.scheduler.drainer)
fires it later through the same chokepoints.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from apps.core.scheduling import RECIPIENT_TZ, to_utc, validate_iana, validate_rrule
from apps.scheduler.models import ScheduledJob

MIN_LEAD = timedelta(seconds=60)
MAX_LEAD = timedelta(days=366)


class SchedulingError(ValueError):
    """Rejected schedule request (past time, too far out, bad timezone/RRULE)."""


def scheduler_enabled() -> bool:
    return getattr(settings, "SCHEDULER_ENABLED", True)


def new_idempotency_key() -> str:
    return uuid.uuid4().hex


def should_schedule(scheduled_at) -> bool:
    """True when ``scheduled_at`` is far enough in the future to defer."""
    if not scheduled_at:
        return False
    if not scheduler_enabled():
        raise SchedulingError("scheduling is currently unavailable")
    return True


def resolve_fire_at(scheduled_at: datetime, tz: str) -> datetime:
    """Normalise ``scheduled_at`` (+ ``tz``) to a validated future UTC instant."""
    tz = tz or "UTC"
    try:
        validate_iana(tz, allow_recipient=True)
    except ValueError as exc:
        raise SchedulingError(str(exc)) from exc

    base_tz = "UTC" if tz == RECIPIENT_TZ else tz
    try:
        fire_at = to_utc(scheduled_at, base_tz)
    except ValueError as exc:
        raise SchedulingError(str(exc)) from exc

    now = timezone.now()
    if fire_at <= now + MIN_LEAD:
        raise SchedulingError(
            "scheduled_at must be at least 60 seconds in the future"
        )
    if fire_at > now + MAX_LEAD:
        raise SchedulingError("scheduled_at is more than a year in the future")
    return fire_at


def _validate_recurrence(recurrence: str) -> str:
    if not recurrence:
        return ""
    try:
        return validate_rrule(recurrence)
    except ValueError as exc:
        raise SchedulingError(str(exc)) from exc


# --- schedule creators -----------------------------------------------------

def schedule_email_message(
    *,
    account,
    payload: dict,
    fire_at: datetime,
    tz: str,
    idempotency_key: str,
    created_by=None,
    recurrence: str = "",
    recurrence_until: datetime | None = None,
    max_occurrences: int | None = None,
) -> ScheduledJob:
    return ScheduledJob.objects.create(
        account=account,
        kind=ScheduledJob.Kind.EMAIL_SINGLE,
        fire_at=fire_at,
        tz=tz or "UTC",
        created_by=created_by,
        template_payload=payload,
        idempotency_key=idempotency_key,
        recurrence=_validate_recurrence(recurrence),
        recurrence_until=recurrence_until,
        max_occurrences=max_occurrences,
    )


def schedule_email_campaign(
    *,
    account,
    campaign,
    payload: dict,
    fire_at: datetime,
    tz: str,
    idempotency_key: str,
    created_by=None,
    recurrence: str = "",
    recurrence_until: datetime | None = None,
    max_occurrences: int | None = None,
) -> ScheduledJob:
    return ScheduledJob.objects.create(
        account=account,
        kind=ScheduledJob.Kind.EMAIL_CAMPAIGN,
        fire_at=fire_at,
        tz=tz or "UTC",
        created_by=created_by,
        template_payload=payload,
        idempotency_key=idempotency_key,
        target_campaign=campaign,
        recurrence=_validate_recurrence(recurrence),
        recurrence_until=recurrence_until,
        max_occurrences=max_occurrences,
    )


def schedule_whatsapp_message(
    *, account, outbound, fire_at: datetime, tz: str, idempotency_key: str, created_by=None
) -> ScheduledJob:
    """Shadow job over a natively-scheduled OutboundMessage (drain_outbound_queue
    already honours its future ``scheduled_at``)."""
    return ScheduledJob.objects.create(
        account=account,
        kind=ScheduledJob.Kind.WHATSAPP,
        fire_at=fire_at,
        tz=tz or "UTC",
        created_by=created_by,
        idempotency_key=idempotency_key,
        target_outbound=outbound,
    )


# --- edit / cancel / list ------------------------------------------------

def reschedule_job(job: ScheduledJob, *, scheduled_at: datetime, tz: str) -> ScheduledJob:
    if job.status != ScheduledJob.Status.SCHEDULED:
        raise SchedulingError(
            f"job {job.public_id} is {job.status} and can no longer be rescheduled"
        )
    fire_at = resolve_fire_at(scheduled_at, tz)
    job.reschedule(fire_at, tz or "UTC")
    return job


def cancel_job(job: ScheduledJob) -> ScheduledJob:
    if job.status != ScheduledJob.Status.SCHEDULED:
        raise SchedulingError(
            f"job {job.public_id} is {job.status} and can no longer be cancelled"
        )
    job.cancel()
    # Cascade to whatever the job pre-created.
    if job.target_campaign_id is not None:
        from apps.email.models import BulkEmailCampaign

        camp = job.target_campaign
        if camp and camp.status == BulkEmailCampaign.Status.SCHEDULED:
            camp.status = BulkEmailCampaign.Status.CANCELLED
            camp.save(update_fields=["status"])
    if job.target_outbound_id is not None:
        from apps.whatsapp.models.outbound import OutboundMessage

        ob = job.target_outbound
        if ob and ob.status == OutboundMessage.Status.QUEUED:
            ob.status = OutboundMessage.Status.CANCELLED
            ob.save(update_fields=["status"])
    return job


def list_jobs(account, *, kind=None, status=None, since=None, until=None):
    qs = ScheduledJob.objects.filter(account=account, parent__isnull=True)
    if kind:
        qs = qs.filter(kind=kind)
    if status:
        qs = qs.filter(status=status)
    if since:
        qs = qs.filter(fire_at__gte=since)
    if until:
        qs = qs.filter(fire_at__lte=until)
    return qs

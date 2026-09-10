"""Drain due ScheduledJob rows — mirrors apps.whatsapp.tasks.drain_outbound_queue.

Split from tasks.py so it is testable without Celery. ``run_due_jobs`` (the beat
task) is a thin wrapper around ``drain()``.

Concurrency safety (three layers):
  * select_for_update(skip_locked=True) — a second drainer skips locked rows.
  * status gate — only SCHEDULED rows are picked; the first tick flips the row
    to PROCESSING inside the locked txn, so a later tick sees PROCESSING.
  * the heavy create_and_queue_* call runs OUTSIDE the lock, after a re-fetch
    that guards on PROCESSING.
"""
from __future__ import annotations

import logging

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from apps.scheduler.models import ScheduledJob

logger = logging.getLogger(__name__)

BATCH = 100


class _PermanentFireError(Exception):
    """Fire failed in a way retrying cannot fix (mark FAILED, no backoff)."""


def _recover_stale() -> int:
    cutoff = timezone.now() - ScheduledJob.PROCESSING_STALE
    stale = ScheduledJob.objects.filter(
        status=ScheduledJob.Status.PROCESSING, updated_at__lt=cutoff
    )
    n = 0
    for job in stale.iterator():
        # If the fire clearly completed before the crash, don't re-fire it.
        if job.target_message_id or (job.result or {}).get("message_public_id"):
            job.mark_sent()
        elif job.attempts >= ScheduledJob.MAX_ATTEMPTS:
            job.mark_failed("stuck in processing", terminal=True)
        else:
            job.status = ScheduledJob.Status.SCHEDULED
            job.save(update_fields=["status", "updated_at"])
        n += 1
    if n:
        logger.warning("scheduler.drain: recovered %s stale PROCESSING jobs", n)
    return n


def _claim_due(now) -> list[int]:
    """Lock and flip a batch of due jobs to PROCESSING; return their ids."""
    ids: list[int] = []
    # skip_locked lets parallel drainers claim disjoint batches; on backends
    # without it (SQLite in tests) fall back to a plain locking select.
    lock_kwargs = (
        {"skip_locked": True}
        if connection.features.has_select_for_update_skip_locked
        else {}
    )
    with transaction.atomic():
        rows = (
            ScheduledJob.objects.select_for_update(**lock_kwargs)
            .filter(status=ScheduledJob.Status.SCHEDULED, fire_at__lte=now)
            .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
            .order_by("fire_at", "id")[:BATCH]
        )
        for job in rows:
            job.status = ScheduledJob.Status.PROCESSING
            job.save(update_fields=["status", "updated_at"])
            ids.append(job.id)
    return ids


def _fire_email_single(job: ScheduledJob) -> dict:
    from apps.api.services import create_and_queue_message

    payload = dict(job.template_payload or {})
    payload.pop("account", None)
    msg = create_and_queue_message(account=job.account, **payload)
    job.target_message = msg
    job.save(update_fields=["target_message", "updated_at"])
    return {"message_public_id": msg.public_id}


def _fire_email_campaign(job: ScheduledJob) -> tuple[dict, bool]:
    """Returns (result, running) — running=True keeps the job in RUNNING."""
    from apps.email.models import BulkEmailCampaign
    from apps.email.tasks import dispatch_campaign

    if job.recurrence:
        # Recurring: build a fresh campaign each occurrence (re-resolve audience).
        from apps.api.services import create_and_queue_campaign

        payload = dict(job.template_payload or {})
        payload.pop("account", None)
        campaign = create_and_queue_campaign(account=job.account, **payload)
        return {"campaign_id": campaign.id}, True

    campaign = job.target_campaign
    if campaign is None:
        raise _PermanentFireError("scheduled campaign no longer exists")
    if campaign.status == BulkEmailCampaign.Status.CANCELLED:
        raise _PermanentFireError("campaign was cancelled")
    if campaign.status == BulkEmailCampaign.Status.SCHEDULED:
        campaign.status = BulkEmailCampaign.Status.QUEUED
        campaign.save(update_fields=["status"])
    # dispatch_campaign no-ops on a non-QUEUED campaign, so a duplicate tick is
    # harmless; call it directly rather than via on_commit (the drainer is not
    # inside the claim lock here).
    dispatch_campaign.delay(campaign.id)
    return {"campaign_id": campaign.id}, True


def _fire_whatsapp(job: ScheduledJob) -> tuple[dict, bool]:
    from apps.whatsapp.models.outbound import OutboundMessage

    ob = job.target_outbound
    if ob is None:
        raise _PermanentFireError("scheduled WhatsApp message no longer exists")
    if ob.status == OutboundMessage.Status.SENT:
        return {"outbound_status": ob.status}, False
    if ob.status in (OutboundMessage.Status.FAILED, OutboundMessage.Status.CANCELLED):
        raise _PermanentFireError(f"WhatsApp message is {ob.status}")
    # Still QUEUED/SENDING — drain_outbound_queue owns the actual send; keep the
    # shadow RUNNING until a later tick sees it terminal.
    return {"outbound_status": ob.status}, True


def _fire_one(job_id: int) -> str:
    from apps.billing.limits import PlanLimitExceeded
    from apps.email.exceptions import UnverifiedDomainError

    job = ScheduledJob.objects.filter(id=job_id).first()
    if job is None or job.status != ScheduledJob.Status.PROCESSING:
        return "skipped"

    # Too-stale guard: never blast a long-missed schedule.
    lateness = (timezone.now() - job.fire_at).total_seconds()
    if lateness > job.stale_grace_secs:
        job.mark_failed("missed schedule window", terminal=True)
        logger.warning("scheduler.drain: job %s stale_skipped (%.0fs late)", job.public_id, lateness)
        return "stale_skipped"

    try:
        running = False
        if job.kind == ScheduledJob.Kind.EMAIL_SINGLE:
            result = _fire_email_single(job)
        elif job.kind == ScheduledJob.Kind.EMAIL_CAMPAIGN:
            result, running = _fire_email_campaign(job)
        elif job.kind == ScheduledJob.Kind.WHATSAPP:
            result, running = _fire_whatsapp(job)
        else:
            raise _PermanentFireError(f"unknown job kind {job.kind!r}")
    except _PermanentFireError as exc:
        job.mark_failed(str(exc), terminal=True)
        return "failed"
    except (UnverifiedDomainError,) as exc:
        job.mark_failed(str(exc), terminal=True)
        return "failed"
    except PlanLimitExceeded as exc:
        job.mark_failed(str(exc))
        return "retried" if job.status == ScheduledJob.Status.SCHEDULED else "failed"
    except Exception as exc:  # noqa: BLE001 - transient provider/infra hiccup
        logger.exception("scheduler.drain: job %s fire error", job.public_id)
        job.mark_failed(str(exc))
        return "retried" if job.status == ScheduledJob.Status.SCHEDULED else "failed"

    job.mark_sent(result, running=running)

    if job.recurrence:
        if job.arm_next_occurrence():
            return "fired_recurring"
        job.status = ScheduledJob.Status.COMPLETED
        job.save(update_fields=["status", "updated_at"])
    return "fired"


def drain() -> dict:
    now = timezone.now()
    _recover_stale()
    claimed = _claim_due(now)

    tally = {"due": len(claimed), "fired": 0, "failed": 0, "retried": 0, "stale_skipped": 0}
    for job_id in claimed:
        outcome = _fire_one(job_id)
        if outcome in ("fired", "fired_recurring"):
            tally["fired"] += 1
        elif outcome in ("failed",):
            tally["failed"] += 1
        elif outcome == "retried":
            tally["retried"] += 1
        elif outcome == "stale_skipped":
            tally["stale_skipped"] += 1

    if tally["due"]:
        logger.info(
            "scheduler.drain: due=%(due)s fired=%(fired)s failed=%(failed)s "
            "retried=%(retried)s stale_skipped=%(stale_skipped)s", tally
        )
    return tally

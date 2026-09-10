"""Celery tasks for the scheduler: the 60s drainer + a daily retention/reconcile."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from apps.scheduler.drainer import drain
from apps.scheduler.models import ScheduledJob

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 30


@shared_task(acks_late=True, reject_on_worker_lost=True, queue="scheduler")
def run_due_jobs():
    return drain()


@shared_task(queue="celery")
def prune_scheduled_jobs():
    now = timezone.now()

    deleted, _ = ScheduledJob.objects.filter(
        status__in=list(ScheduledJob.TERMINAL_STATUSES),
        updated_at__lt=now - timedelta(days=_RETENTION_DAYS),
    ).delete()

    # Reconcile: a RUNNING campaign job whose campaign has completed.
    from apps.email.models import BulkEmailCampaign

    running = ScheduledJob.objects.filter(
        status=ScheduledJob.Status.RUNNING,
        kind=ScheduledJob.Kind.EMAIL_CAMPAIGN,
        target_campaign__isnull=False,
    ).select_related("target_campaign")
    reconciled = 0
    for job in running.iterator():
        camp = job.target_campaign
        if camp and camp.status in (
            BulkEmailCampaign.Status.COMPLETED,
            BulkEmailCampaign.Status.FAILED,
            BulkEmailCampaign.Status.CANCELLED,
        ):
            job.status = (
                ScheduledJob.Status.COMPLETED
                if camp.status == BulkEmailCampaign.Status.COMPLETED
                else ScheduledJob.Status.FAILED
            )
            job.save(update_fields=["status", "updated_at"])
            reconciled += 1

    # Orphan guard: a SCHEDULED campaign with no live job, older than 1h.
    orphans = BulkEmailCampaign.objects.filter(
        status=BulkEmailCampaign.Status.SCHEDULED,
        created_at__lt=now - timedelta(hours=1),
    ).exclude(
        scheduled_jobs__status__in=list(ScheduledJob.ACTIVE_STATUSES)
    )
    orphaned = orphans.update(
        status=BulkEmailCampaign.Status.FAILED, error="orphaned scheduled campaign"
    )

    logger.info(
        "prune_scheduled_jobs: deleted=%s reconciled=%s orphaned=%s",
        deleted, reconciled, orphaned,
    )
    return {"deleted": deleted, "reconciled": reconciled, "orphaned": orphaned}

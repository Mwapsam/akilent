"""Maintenance jobs for the observability stores."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)

_DEFAULT_EVENT_RETENTION_DAYS = 90
_IDEMPOTENCY_TTL_HOURS = 24
_API_REQUEST_RETENTION_DAYS = 30


@shared_task
def prune_message_events() -> int:
    """Delete MessageEvent rows past each account's log-retention window."""
    from apps.accounts.models import Account
    from apps.logs.models import MessageEvent

    deleted = 0
    for account in Account.objects.all().only("id"):
        days = _retention_days_for(account)
        cutoff = timezone.now() - timedelta(days=days)
        n, _ = MessageEvent.objects.filter(account=account, occurred_at__lt=cutoff).delete()
        deleted += n
    if deleted:
        logger.info("prune_message_events: deleted %s rows", deleted)
    return deleted


def _retention_days_for(account) -> int:
    try:
        return int(account.subscription.plan.log_retention_days) or _DEFAULT_EVENT_RETENTION_DAYS
    except Exception:  # noqa: BLE001
        return _DEFAULT_EVENT_RETENTION_DAYS


@shared_task
def prune_idempotency_records() -> int:
    from apps.logs.models import IdempotencyRecord

    cutoff = timezone.now() - timedelta(hours=_IDEMPOTENCY_TTL_HOURS)
    n, _ = IdempotencyRecord.objects.filter(created_at__lt=cutoff).delete()
    if n:
        logger.info("prune_idempotency_records: deleted %s rows", n)
    return n


@shared_task
def prune_api_requests() -> int:
    from apps.logs.models import ApiRequest

    cutoff = timezone.now() - timedelta(days=_API_REQUEST_RETENTION_DAYS)
    n, _ = ApiRequest.objects.filter(created_at__lt=cutoff).delete()
    if n:
        logger.info("prune_api_requests: deleted %s rows", n)
    return n


@shared_task
def reconcile_message_stats(day_iso: str | None = None) -> int:
    """Recompute MessageStatsDaily for a day (default: yesterday)."""
    from apps.logs.stats import reconcile

    if day_iso:
        day = timezone.datetime.fromisoformat(day_iso).date()
    else:
        day = (timezone.now() - timedelta(days=1)).date()
    written = reconcile(day)
    logger.info("reconcile_message_stats: %s rows for %s", written, day)
    return written

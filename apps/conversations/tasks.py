"""Celery tasks for the conversation spine."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="celery")
def remind_missed_conversations() -> int:
    """Give every missed WhatsApp conversation a due follow-up (see ``recovery``)."""
    from apps.conversations.recovery import create_missed_followups

    n = create_missed_followups()
    if n:
        logger.info("remind_missed_conversations: created %d follow-up(s)", n)
    return n


@shared_task(queue="celery")
def capture_benchmarks() -> int:
    """Measure each business's starting week and day-30 week once they've closed (``benchmarks``)."""
    from apps.conversations import benchmarks
    from apps.whatsapp import api as whatsapp_api

    made = 0
    for account, connected_at in whatsapp_api.connected_accounts():
        try:
            made += len(benchmarks.capture(account, connected_at))
        except Exception:
            logger.exception("capture_benchmarks: failed for account %s", account.pk)
    return made


@shared_task(queue="celery")
def capture_weekly_snapshots() -> int:
    """Measure each business's closed, settled weeks for the Momentum trend (``snapshots``)."""
    from apps.conversations import snapshots
    from apps.whatsapp import api as whatsapp_api

    made = 0
    for account, connected_at in whatsapp_api.connected_accounts():
        try:
            made += len(snapshots.capture(account, connected_at))
        except Exception:
            logger.exception(
                "capture_weekly_snapshots: failed for account %s", account.pk
            )
    return made


@shared_task(queue="celery")
def send_weekly_reports() -> int:
    """Email each connected business's owners the week's proof bar and numbers, once per
    (account, week) — see ``weekly_report``. Business Health's retention mechanic.

    Scheduled daily like ``capture_weekly_snapshots``: most days there is no newly settled week
    to report on, and the ``Event`` record below stops a business being emailed twice for the
    same week even if the task overlaps a retry.
    """
    from django.utils import timezone

    from apps.accounts.notifications import recipients
    from apps.billing.limits import LimitChecker
    from apps.conversations import weekly_report
    from apps.conversations.models import Event, InsightSettings
    from apps.email.services.send import send_system_email
    from apps.whatsapp import api as whatsapp_api

    now = timezone.now()
    week = weekly_report.report_week(now)
    if week is None:
        return 0
    week_key = week.start.date().isoformat()

    sent = 0
    for account, _connected_at in whatsapp_api.connected_accounts():
        settings_row = InsightSettings.objects.filter(account=account).first()
        if settings_row is not None and not settings_row.weekly_report:
            continue
        if Event.objects.filter(
            account=account, source="weekly_report", source_event_id=week_key
        ).exists():
            continue
        try:
            full = LimitChecker(account).has_feature("detailed_analytics")
            email = weekly_report.build_email(account, week, full=full)
            if email is None:
                continue
            for user in recipients(account, "owners"):
                if not user.email:
                    continue
                send_system_email(
                    to_email=user.email,
                    subject=email["subject"],
                    text_body=email["text_body"],
                )
            Event.objects.create(
                account=account,
                type="weekly_report_sent",
                occurred_at=now,
                source="weekly_report",
                source_event_id=week_key,
            )
            sent += 1
        except Exception:
            logger.exception("send_weekly_reports: failed for account %s", account.pk)
    return sent

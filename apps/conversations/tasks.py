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
def escalate_overdue_conversations() -> dict:
    """Every OPEN, still-waiting conversation past ``OVERDUE_WAITING`` gets one
    ``conversation.escalated`` Event per waiting episode (Phase B.2). A still-
    unassigned one also gets a fresh routing attempt — a team or agent may
    have become available since the customer wrote in. An assigned one just
    gets the Event for now: the notification mechanism beyond the audit
    trail is deliberately out of scope until there's a real need for it.

    Idempotent via ``emit_event``'s ``source_event_id`` (keyed on the
    conversation and the exact ``last_in`` it's waiting on): a customer's
    later message changes ``last_in`` and starts a new, escalatable episode;
    re-running this task on the same episode escalates nothing twice.
    """
    from django.utils import timezone

    from apps.conversations.models import Conversation
    from apps.conversations.services import emit_event, route_new_conversation
    from apps.conversations.state import OVERDUE_WAITING, unanswered_q, with_activity

    now = timezone.now()
    cutoff = now - OVERDUE_WAITING
    overdue = with_activity(
        Conversation.objects.filter(status=Conversation.Status.OPEN)
    ).filter(unanswered_q(), last_in__lte=cutoff)

    escalated = 0
    for conversation in overdue.select_related("account"):
        event = emit_event(
            account=conversation.account,
            type="conversation.escalated",
            occurred_at=now,
            source="conversations",
            source_event_id=f"escalate:{conversation.pk}:{conversation.last_in.isoformat()}",
            payload={
                "conversation_id": conversation.public_id,
                "assigned_to_id": conversation.assigned_to_id,
                "assigned_team_id": conversation.assigned_team_id,
            },
            subject_type="conversation",
            subject_id=str(conversation.pk),
        )
        if event is None:
            continue  # already escalated this waiting episode
        escalated += 1
        if conversation.assigned_to_id is None:
            route_new_conversation(conversation)
    if escalated:
        logger.warning("escalate_overdue_conversations: escalated=%s", escalated)
    return {"escalated": escalated}


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

    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives

    from apps.accounts.notifications import recipients
    from apps.billing.limits import LimitChecker
    from apps.conversations import weekly_report
    from apps.conversations.models import Event, InsightSettings
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
                msg = EmailMultiAlternatives(
                    subject=email["subject"],
                    body=email["text_body"],
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=[user.email],
                )
                if email.get("html_body"):
                    msg.attach_alternative(email["html_body"], "text/html")
                msg.send()
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

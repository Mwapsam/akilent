"""Celery tasks for the conversation spine."""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(queue="celery")
def send_typing_indicator(conversation_id: int) -> bool:
    """Show the customer "typing…" while a teammate writes a reply (WhatsApp, Instagram).

    Best effort and cosmetic, like the read receipt: anything missing or failing is
    dropped quietly. Meta clears the indicator after ~25 seconds or when the reply lands.
    """
    from apps.conversations.models import Conversation, Message

    try:
        conversation = Conversation.objects.select_related("account").get(
            pk=conversation_id
        )
        inbound = Message.objects.filter(
            conversation=conversation, direction=Message.Direction.INBOUND
        ).order_by("-timestamp", "-id")
        if conversation.channel == Conversation.Channel.WHATSAPP:
            last = (
                inbound.filter(whatsapp_message__isnull=False)
                .select_related("whatsapp_message")
                .first()
            )
            wa_message = last.whatsapp_message if last is not None else None
            if wa_message is None or not wa_message.message_id:
                return False
            from apps.whatsapp.tasks import _get_provider_for_account

            provider = _get_provider_for_account(conversation.account)
            if provider is None:
                return False
            return provider.send_typing_indicator(wa_message.message_id).success
        if conversation.channel == Conversation.Channel.INSTAGRAM:
            from apps.core.channels import channel_state

            if channel_state("instagram") != "enabled":
                return False
            last = (
                inbound.filter(instagram_message__conversation__isnull=False)
                .select_related(
                    "instagram_message__conversation__instagram_account",
                    "instagram_message__conversation__instagram_contact",
                )
                .first()
            )
            ig_message = last.instagram_message if last is not None else None
            ig_conversation = (
                ig_message.conversation if ig_message is not None else None
            )
            if ig_conversation is None:
                return False
            ig_account = ig_conversation.instagram_account
            if not ig_account.access_token:
                return False
            from apps.instagram.providers.meta import MetaInstagramProvider

            provider = MetaInstagramProvider(
                access_token=ig_account.access_token,
                instagram_account_id=ig_account.instagram_business_account_id,
            )
            return provider.send_typing(
                ig_conversation.instagram_contact.instagram_scoped_id
            )
    except Exception as exc:
        logger.debug("send_typing_indicator: skipped for %s: %s", conversation_id, exc)
    return False


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
    have become available since the customer wrote in. Then whoever has it
    (the assignee, or the owners and admins if nobody does) gets one email
    for that episode, so a waiting customer isn't left to the audit trail.

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
    for conversation in overdue.select_related("account", "contact"):
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
            conversation.refresh_from_db(fields=["assigned_to"])
        _alert_overdue(conversation, now)
    if escalated:
        logger.warning("escalate_overdue_conversations: escalated=%s", escalated)
    return {"escalated": escalated}


def _alert_overdue(conversation, now) -> None:
    """Email whoever has this conversation that the customer is still waiting.

    Best-effort: a mail failure must not stop the escalation sweep.
    """
    from django.urls import reverse

    from apps.accounts.notifications import notify_team

    contact = conversation.contact
    who = contact.full_name or str(contact)
    hours = max(int((now - conversation.last_in).total_seconds() // 3600), 1)
    waited = f"{hours} hour" if hours == 1 else f"{hours} hours"
    try:
        notify_team(
            conversation.account,
            to="assignee",
            conversation=conversation,
            subject=f"{who} has been waiting {waited} for a reply",
            text=(
                f"{who} messaged on {conversation.get_channel_display()} and hasn't had a "
                f"reply for {waited}."
            ),
            path=reverse("conversations:detail", args=[conversation.public_id]),
            reason=(
                "You're getting this because Akilent tells the assigned teammate (or the "
                "owners, if nobody is assigned) when a customer waits too long."
            ),
        )
    except Exception:
        logger.exception(
            "escalate_overdue_conversations: alert failed for conversation=%s",
            conversation.pk,
        )


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
    from django.conf import settings
    from django.core.mail import EmailMultiAlternatives
    from django.utils import timezone

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

"""SLA lifecycle service.

apply()              — set sla_due_at on a newly created ticket
is_at_risk()         — True if SLA due within escalation_after_minutes
is_breached()        — True if now > sla_due_at and ticket not resolved/closed
mark_breached()      — idempotent; sets flag + records event + queues notification
evaluate_open_tickets() — called by Celery task every 5 min
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"resolved", "closed"}


def apply(ticket) -> None:
    """Look up the matching SLAPolicy and set sla_due_at."""
    from apps.support.models import SLAPolicy

    try:
        policy = SLAPolicy.objects.get(
            customer_tier=ticket.customer_tier,
            priority=ticket.priority,
            is_active=True,
        )
    except SLAPolicy.DoesNotExist:
        logger.warning(
            "sla.apply: no policy for tier=%s priority=%s ticket=%s",
            ticket.customer_tier,
            ticket.priority,
            ticket.pk,
        )
        return

    ticket.sla_policy = policy
    ticket.sla_due_at = timezone.now() + timedelta(
        minutes=policy.first_response_minutes
    )
    ticket.save(update_fields=["sla_policy", "sla_due_at", "updated_at"])


def is_at_risk(ticket) -> bool:
    """True when SLA will breach within escalation_after_minutes."""
    if ticket.sla_due_at is None or ticket.status in _TERMINAL_STATUSES:
        return False
    if (
        ticket.sla_policy_id is None
        or ticket.sla_policy.escalation_after_minutes is None
    ):
        return False
    remaining = (ticket.sla_due_at - timezone.now()).total_seconds() / 60
    return 0 < remaining <= ticket.sla_policy.escalation_after_minutes


def is_breached(ticket) -> bool:
    """True when SLA has expired and ticket is still open."""
    if ticket.sla_due_at is None or ticket.status in _TERMINAL_STATUSES:
        return False
    return timezone.now() > ticket.sla_due_at


def mark_breached(ticket) -> None:
    """Idempotent — no-op if already marked breached."""
    if ticket.sla_breached:
        return

    from django.db import transaction

    from apps.support.models import SupportEvent
    from apps.support.services import notifications, record_event

    ticket.sla_breached = True
    ticket.save(update_fields=["sla_breached", "updated_at"])
    record_event(ticket, SupportEvent.SLA_BREACHED)
    transaction.on_commit(lambda: notifications.on_sla_breach(ticket))


def evaluate_open_tickets() -> tuple[int, int]:
    """Scan open tickets; mark breached; trigger escalation for at-risk.

    Returns (at_risk_count, breached_count).
    """
    from apps.support.models import SupportTicket
    from apps.support.services import escalation as escalation_service

    open_qs = (
        SupportTicket.objects.filter(
            sla_due_at__isnull=False,
        )
        .exclude(status__in=_TERMINAL_STATUSES)
        .select_related("sla_policy", "category")
    )

    at_risk_count = 0
    breached_count = 0

    for ticket in open_qs.iterator():
        try:
            if is_breached(ticket):
                mark_breached(ticket)
                breached_count += 1
            elif is_at_risk(ticket):
                escalation_service.evaluate(ticket)
                at_risk_count += 1
        except Exception:
            logger.exception("sla.evaluate_open_tickets: error on ticket=%s", ticket.pk)

    return at_risk_count, breached_count

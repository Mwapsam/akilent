"""Deterministic routing: assign queue and initial support level from ticket data."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Category slug → queue slug.  Falls through to "general" when no match.
_CATEGORY_TO_QUEUE: dict[str, str] = {
    "payments": "payments",
    "payment-failed": "payments",
    "payment-pending": "payments",
    "payment-reversed": "payments",
    "settlement": "payments",
    "reconciliation": "payments",
    "billing": "billing",
    "whatsapp": "whatsapp",
    "whatsapp-connection": "whatsapp",
    "whatsapp-message-delivery": "whatsapp",
    "whatsapp-verification": "whatsapp",
    "email": "email",
    "technical": "technical",
    "technical-bugs": "technical",
    "technical-performance": "technical",
    "technical-downtime": "technical",
    "technical-data": "technical",
    "security": "security",
    "account": "account",
    "account-login": "account",
    "account-password": "account",
    "account-mfa": "account",
    "account-team": "account",
    "onboarding": "account",
    "integrations": "integrations",
}


def route_ticket(ticket) -> None:
    """Assign queue and initial support_level; record assignment event."""
    from apps.support.models import SupportAssignment, SupportEvent, SupportQueue
    from apps.support.services import record_event

    # Determine queue from category slug
    category_slug = ticket.category.slug if ticket.category_id else None
    queue_slug = _CATEGORY_TO_QUEUE.get(category_slug or "", "general")

    try:
        queue = SupportQueue.objects.get(slug=queue_slug, is_active=True)
    except SupportQueue.DoesNotExist:
        try:
            queue = SupportQueue.objects.get(slug="general", is_active=True)
        except SupportQueue.DoesNotExist:
            queue = None

    # Tier 3+ with P1/P2 start at L2
    from apps.support.models.sla import SLAPolicy

    tier = ticket.customer_tier
    priority = ticket.priority
    if tier >= SLAPolicy.TIER_3 and priority in (SLAPolicy.P1, SLAPolicy.P2):
        support_level = "l2"
    else:
        support_level = "l1"

    ticket.queue = queue
    ticket.support_level = support_level
    ticket.save(update_fields=["queue", "support_level", "updated_at"])

    SupportAssignment.objects.create(
        ticket=ticket,
        from_agent=None,
        to_agent=None,
        assigned_by=None,
        notes=f"Auto-routed to {queue_slug} queue at {support_level.upper()}",
    )
    record_event(
        ticket,
        SupportEvent.ASSIGNED,
        actor=None,
        queue=queue_slug,
        support_level=support_level,
    )

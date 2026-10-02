"""Escalation engine.

evaluate(ticket)  — apply matching rules; returns True if ticket was escalated
escalate(...)     — idempotent single escalation step

Rules are in-memory config data (not hard-coded if/else).
Every escalation produces a SupportEscalation row + SupportEvent + notification.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------
# Each rule has:
#   conditions  — dict of {field: value} or callable(ticket) -> bool
#   to_level    — target support_level string
#   reason      — SupportEscalation.REASON_CHOICES value
# ---------------------------------------------------------------------------


def _sla_remaining_lt(minutes: int):
    def check(ticket) -> bool:
        if ticket.sla_due_at is None:
            return False
        remaining = (ticket.sla_due_at - timezone.now()).total_seconds() / 60
        return 0 < remaining < minutes

    return check


ESCALATION_RULES: list[dict[str, Any]] = [
    # T4/P1 at-risk (< 15 min remaining) → L2
    {
        "condition": lambda t: (
            t.customer_tier >= 4 and t.priority == "p1" and _sla_remaining_lt(15)(t)
        ),
        "to_level": "l2",
        "reason": "sla_breach",
    },
    # T3/P1 at-risk (< 30 min remaining) → L2
    {
        "condition": lambda t: (
            t.customer_tier == 3 and t.priority == "p1" and _sla_remaining_lt(30)(t)
        ),
        "to_level": "l2",
        "reason": "sla_breach",
    },
    # T4/P2 at-risk (< 30 min remaining) → L2
    {
        "condition": lambda t: (
            t.customer_tier >= 4 and t.priority == "p2" and _sla_remaining_lt(30)(t)
        ),
        "to_level": "l2",
        "reason": "tier_rule",
    },
    # Security category always goes to L2
    {
        "condition": lambda t: (
            t.category_id is not None
            and t.category.slug in ("security",)
            and t.support_level == "l1"
        ),
        "to_level": "l2",
        "reason": "security",
    },
    # T3+ / P1 baseline → L2 (initial routing fallback for late categorisation)
    {
        "condition": lambda t: (
            t.customer_tier >= 3 and t.priority == "p1" and t.support_level == "l1"
        ),
        "to_level": "l2",
        "reason": "tier_rule",
    },
]


def evaluate(ticket) -> bool:
    """Apply all matching escalation rules. Returns True if any rule fired."""
    escalated = False
    for rule in ESCALATION_RULES:
        try:
            if rule["condition"](ticket):
                escalate(
                    ticket=ticket,
                    to_level=rule["to_level"],
                    reason=rule["reason"],
                )
                escalated = True
        except Exception:
            logger.exception(
                "escalation.evaluate: rule error ticket=%s reason=%s",
                ticket.pk,
                rule.get("reason"),
            )
    return escalated


def escalate(
    *, ticket, to_level: str, reason: str, notes: str = "", actor=None
) -> None:
    """Record one escalation step.  Idempotent: no-op if already at target level,
    or if an identical escalation was recorded within the last hour (Celery guard).
    """
    from django.db import transaction

    from apps.support.models import SupportEscalation, SupportEvent
    from apps.support.services import notifications, record_event

    # Guard 1: already at or beyond target level
    level_order = {"l1": 1, "l2": 2, "l3": 3, "l4": 4}
    if level_order.get(ticket.support_level, 0) >= level_order.get(to_level, 0):
        return

    # Guard 2: prevent duplicate rows from repeated Celery runs
    cutoff = timezone.now() - timedelta(hours=1)
    already = SupportEscalation.objects.filter(
        ticket=ticket,
        from_level=ticket.support_level,
        to_level=to_level,
        reason=reason,
        created_at__gte=cutoff,
    ).exists()
    if already:
        return

    from_level = ticket.support_level

    SupportEscalation.objects.create(
        ticket=ticket,
        from_level=from_level,
        to_level=to_level,
        reason=reason,
        notes=notes,
        escalated_by=actor,
    )

    ticket.support_level = to_level
    ticket.save(update_fields=["support_level", "updated_at"])

    record_event(
        ticket,
        SupportEvent.ESCALATED,
        actor=actor,
        **{"from": from_level, "to": to_level, "reason": reason},
    )

    transaction.on_commit(lambda: notifications.on_escalation(ticket))

"""Core ticket service — all SupportTicket mutations go through here."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from apps.support.models.ticket import SupportTicket

if TYPE_CHECKING:
    from apps.support.models import (
        SupportInternalNote,
        SupportMessage,
        SupportTicketReference,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Authoritative status transition matrix
# No view, task, or external service may bypass this.
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[str, set[str]] = {
    SupportTicket.NEW: {SupportTicket.TRIAGED},
    SupportTicket.TRIAGED: {SupportTicket.ASSIGNED},
    SupportTicket.ASSIGNED: {SupportTicket.IN_PROGRESS},
    SupportTicket.IN_PROGRESS: {
        SupportTicket.WAITING_CUSTOMER,
        SupportTicket.WAITING_INTERNAL,
        SupportTicket.ESCALATED,
        SupportTicket.RESOLVED,
    },
    SupportTicket.WAITING_CUSTOMER: {SupportTicket.IN_PROGRESS, SupportTicket.RESOLVED},
    SupportTicket.WAITING_INTERNAL: {SupportTicket.IN_PROGRESS},
    SupportTicket.ESCALATED: {SupportTicket.IN_PROGRESS, SupportTicket.RESOLVED},
    SupportTicket.RESOLVED: {SupportTicket.CLOSED, SupportTicket.REOPENED},
    SupportTicket.CLOSED: {SupportTicket.REOPENED},
    SupportTicket.REOPENED: {SupportTicket.IN_PROGRESS},
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_ticket(
    *,
    account,
    subject: str,
    description: str,
    submitted_by=None,
    category_slug: str | None = None,
    customer_tier: int = 1,
    priority: str = "p3",
) -> SupportTicket:
    """Create a ticket and run routing + SLA in one atomic operation."""
    from apps.support.models import SupportCategory, SupportEvent
    from apps.support.services import notifications, record_event, routing
    from apps.support.services import sla as sla_service

    with transaction.atomic():
        category = None
        if category_slug:
            category = SupportCategory.objects.filter(slug=category_slug).first()

        ticket = SupportTicket.objects.create(
            account=account,
            submitted_by=submitted_by,
            category=category,
            customer_tier=customer_tier,
            priority=priority,
            subject=subject,
            description=description,
            status=SupportTicket.NEW,
        )

        routing.route_ticket(ticket)
        sla_service.apply(ticket)
        record_event(ticket, SupportEvent.CREATED, actor=submitted_by)

        transaction.on_commit(lambda: notifications.on_ticket_created(ticket))

    return ticket


def update_status(*, ticket: SupportTicket, new_status: str, actor=None) -> None:
    """Move ticket to new_status, enforcing the transition matrix."""
    from django.utils import timezone

    from apps.support.models import SupportEvent
    from apps.support.services import record_event

    allowed = VALID_TRANSITIONS.get(ticket.status, set())
    if new_status not in allowed:
        raise ValueError(
            f"Invalid transition: {ticket.status!r} → {new_status!r} "
            f"(allowed: {sorted(allowed)})"
        )

    old_status = ticket.status
    ticket.status = new_status

    save_fields = ["status", "updated_at"]
    if new_status == SupportTicket.RESOLVED:
        ticket.resolved_at = timezone.now()
        save_fields.append("resolved_at")
    elif new_status == SupportTicket.CLOSED:
        ticket.closed_at = timezone.now()
        save_fields.append("closed_at")
    elif new_status == SupportTicket.REOPENED:
        ticket.resolved_at = None
        ticket.closed_at = None
        save_fields += ["resolved_at", "closed_at"]

    ticket.save(update_fields=save_fields)
    record_event(
        ticket,
        SupportEvent.STATUS_CHANGED,
        actor=actor,
        **{"from": old_status, "to": new_status},
    )


def set_priority(*, ticket: SupportTicket, new_priority: str, actor=None) -> None:
    from apps.support.models import SupportEvent
    from apps.support.services import record_event
    from apps.support.services import sla as sla_service

    old_priority = ticket.priority
    ticket.priority = new_priority
    ticket.save(update_fields=["priority", "updated_at"])
    sla_service.apply(ticket)
    record_event(
        ticket,
        SupportEvent.PRIORITY_CHANGED,
        actor=actor,
        **{"from": old_priority, "to": new_priority},
    )


def resolve_ticket(*, ticket: SupportTicket, actor=None) -> None:
    update_status(ticket=ticket, new_status=SupportTicket.RESOLVED, actor=actor)


def close_ticket(*, ticket: SupportTicket, actor=None) -> None:
    update_status(ticket=ticket, new_status=SupportTicket.CLOSED, actor=actor)


def reopen_ticket(*, ticket: SupportTicket, actor=None) -> None:
    update_status(ticket=ticket, new_status=SupportTicket.REOPENED, actor=actor)


def assign_agent(*, ticket: SupportTicket, agent, actor=None, notes: str = "") -> None:
    from apps.support.models import SupportAssignment, SupportEvent
    from apps.support.services import record_event

    old_agent = ticket.assigned_agent
    ticket.assigned_agent = agent
    ticket.save(update_fields=["assigned_agent", "updated_at"])

    SupportAssignment.objects.create(
        ticket=ticket,
        from_agent=old_agent,
        to_agent=agent,
        assigned_by=actor,
        notes=notes,
    )
    record_event(
        ticket, SupportEvent.ASSIGNED, actor=actor, to_agent=agent.pk if agent else None
    )


def add_message(
    *,
    ticket: SupportTicket,
    body: str,
    author=None,
    is_from_customer: bool = False,
) -> SupportMessage:
    from django.utils import timezone

    from apps.support.models import SupportEvent, SupportMessage
    from apps.support.services import record_event

    msg = SupportMessage.objects.create(
        ticket=ticket,
        author=author,
        is_from_customer=is_from_customer,
        body=body,
    )

    # Record first agent response time
    if not is_from_customer and ticket.first_response_at is None:
        ticket.first_response_at = timezone.now()
        ticket.save(update_fields=["first_response_at", "updated_at"])
        record_event(ticket, SupportEvent.FIRST_RESPONSE, actor=author)

    return msg


def add_internal_note(
    *,
    ticket: SupportTicket,
    body: str,
    author=None,
) -> SupportInternalNote:
    from apps.support.models import SupportEvent, SupportInternalNote
    from apps.support.services import record_event

    note = SupportInternalNote.objects.create(
        ticket=ticket,
        author=author,
        body=body,
    )
    record_event(ticket, SupportEvent.NOTE_ADDED, actor=author)
    return note


def add_reference(
    *,
    ticket: SupportTicket,
    obj,
    relationship: str = "related",
    label: str = "",
) -> SupportTicketReference:
    from django.contrib.contenttypes.models import ContentType

    from apps.support.models import SupportEvent, SupportTicketReference
    from apps.support.services import record_event

    ct = ContentType.objects.get_for_model(obj)
    ref, _ = SupportTicketReference.objects.get_or_create(
        ticket=ticket,
        content_type=ct,
        object_id=str(obj.pk),
        defaults={"relationship": relationship, "label": label},
    )
    record_event(
        ticket,
        SupportEvent.REFERENCE_ADDED,
        content_type=ct.model,
        object_id=str(obj.pk),
    )
    return ref

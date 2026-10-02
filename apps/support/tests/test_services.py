"""Service-layer tests: routing, SLA, escalation, status transitions, account isolation."""

import pytest
from django.utils import timezone

from apps.support.models import (
    SupportEscalation,
    SupportEvent,
    SupportTicket,
)
from apps.support.services import ticket as ticket_service

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ticket(account, **kwargs):
    defaults = dict(subject="Test", description="Desc", customer_tier=1, priority="p3")
    defaults.update(kwargs)
    return ticket_service.create_ticket(account=account, **defaults)


# ---------------------------------------------------------------------------
# Ticket creation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_ticket_has_created_event(account, general_queue):
    ticket = _make_ticket(account)
    assert ticket.pk is not None
    assert SupportEvent.objects.filter(
        ticket=ticket, event_type=SupportEvent.CREATED
    ).exists()


@pytest.mark.django_db
def test_create_ticket_applies_sla(account, sla_t1_p1, general_queue):
    ticket = _make_ticket(account, priority="p1", customer_tier=1)
    assert ticket.sla_due_at is not None
    assert ticket.sla_policy_id == sla_t1_p1.pk


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_routing_sends_payments_to_payments_queue(
    account, payments_category, payments_queue, general_queue
):
    ticket = _make_ticket(account, category_slug="payments")
    assert ticket.queue == payments_queue


@pytest.mark.django_db
def test_routing_falls_back_to_general(account, general_queue):
    ticket = _make_ticket(account)  # no category
    assert ticket.queue == general_queue


@pytest.mark.django_db
def test_routing_t3_p1_starts_at_l2(account, sla_t3_p1, general_queue):
    ticket = _make_ticket(account, customer_tier=3, priority="p1")
    assert ticket.support_level == "l2"


@pytest.mark.django_db
def test_routing_t1_p1_starts_at_l1(account, sla_t1_p1, general_queue):
    ticket = _make_ticket(account, customer_tier=1, priority="p1")
    assert ticket.support_level == "l1"


# ---------------------------------------------------------------------------
# Status transition matrix
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_valid_transition_new_to_triaged(account, general_queue):
    ticket = _make_ticket(account)
    assert ticket.status == SupportTicket.NEW
    ticket_service.update_status(ticket=ticket, new_status=SupportTicket.TRIAGED)
    ticket.refresh_from_db()
    assert ticket.status == SupportTicket.TRIAGED


@pytest.mark.django_db
def test_invalid_transition_raises(account, general_queue):
    ticket = _make_ticket(account)
    with pytest.raises(ValueError, match="Invalid transition"):
        ticket_service.update_status(ticket=ticket, new_status=SupportTicket.CLOSED)


@pytest.mark.django_db
def test_full_lifecycle(account, general_queue):
    """Walk a ticket through the canonical happy path."""
    ticket = _make_ticket(account)
    for status in [
        SupportTicket.TRIAGED,
        SupportTicket.ASSIGNED,
        SupportTicket.IN_PROGRESS,
        SupportTicket.RESOLVED,
        SupportTicket.CLOSED,
    ]:
        ticket_service.update_status(ticket=ticket, new_status=status)
    ticket.refresh_from_db()
    assert ticket.status == SupportTicket.CLOSED
    assert ticket.closed_at is not None


@pytest.mark.django_db
def test_reopen_clears_timestamps(account, general_queue):
    ticket = _make_ticket(account)
    for s in [
        SupportTicket.TRIAGED,
        SupportTicket.ASSIGNED,
        SupportTicket.IN_PROGRESS,
        SupportTicket.RESOLVED,
        SupportTicket.CLOSED,
    ]:
        ticket_service.update_status(ticket=ticket, new_status=s)
    ticket_service.update_status(ticket=ticket, new_status=SupportTicket.REOPENED)
    ticket.refresh_from_db()
    assert ticket.resolved_at is None
    assert ticket.closed_at is None


# ---------------------------------------------------------------------------
# SLA
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_mark_breached_is_idempotent(account, sla_t1_p1, general_queue):
    from apps.support.services import sla as sla_service

    ticket = _make_ticket(account, priority="p1", customer_tier=1)
    ticket.sla_due_at = timezone.now() - timezone.timedelta(minutes=1)
    ticket.save()

    sla_service.mark_breached(ticket)
    sla_service.mark_breached(ticket)  # second call must be a no-op

    breach_events = SupportEvent.objects.filter(
        ticket=ticket, event_type=SupportEvent.SLA_BREACHED
    )
    assert breach_events.count() == 1


@pytest.mark.django_db
def test_is_breached_false_for_resolved(account, sla_t1_p1, general_queue):
    from apps.support.services import sla as sla_service

    ticket = _make_ticket(account, priority="p1", customer_tier=1)
    ticket.sla_due_at = timezone.now() - timezone.timedelta(minutes=1)
    ticket.status = SupportTicket.RESOLVED
    ticket.save()

    assert not sla_service.is_breached(ticket)


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_escalation_idempotent(account, sla_t4_p1, general_queue):
    from apps.support.services import escalation as esc_service

    ticket = _make_ticket(account, customer_tier=4, priority="p1")
    # Manually set l1 since routing may have put it at l2 already
    ticket.support_level = "l1"
    ticket.save()

    esc_service.escalate(ticket=ticket, to_level="l2", reason="tier_rule")
    esc_service.escalate(ticket=ticket, to_level="l2", reason="tier_rule")  # duplicate

    assert SupportEscalation.objects.filter(ticket=ticket).count() == 1


@pytest.mark.django_db
def test_escalation_no_downgrade(account, general_queue):
    from apps.support.services import escalation as esc_service

    ticket = _make_ticket(account)
    ticket.support_level = "l2"
    ticket.save()

    esc_service.escalate(ticket=ticket, to_level="l1", reason="tier_rule")

    assert SupportEscalation.objects.filter(ticket=ticket).count() == 0
    ticket.refresh_from_db()
    assert ticket.support_level == "l2"


# ---------------------------------------------------------------------------
# Messages and first-response tracking
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_first_agent_message_sets_first_response_at(account, agent, general_queue):
    ticket = _make_ticket(account)
    assert ticket.first_response_at is None
    ticket_service.add_message(ticket=ticket, body="Hi", author=agent)
    ticket.refresh_from_db()
    assert ticket.first_response_at is not None

    # Second message should not overwrite it
    original = ticket.first_response_at
    ticket_service.add_message(ticket=ticket, body="Follow up", author=agent)
    ticket.refresh_from_db()
    assert ticket.first_response_at == original


@pytest.mark.django_db
def test_customer_message_does_not_set_first_response_at(account, general_queue):
    ticket = _make_ticket(account)
    ticket_service.add_message(ticket=ticket, body="Help!", is_from_customer=True)
    ticket.refresh_from_db()
    assert ticket.first_response_at is None


# ---------------------------------------------------------------------------
# Account isolation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_tickets_are_account_scoped(account, another_account, general_queue):
    t1 = _make_ticket(account)
    t2 = _make_ticket(another_account)

    assert not SupportTicket.objects.filter(account=account, pk=t2.pk).exists()
    assert not SupportTicket.objects.filter(account=another_account, pk=t1.pk).exists()


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_add_reference_is_idempotent(account, general_queue):
    from apps.support.models import SupportTicketReference

    ticket = _make_ticket(account)
    ticket_service.add_reference(ticket=ticket, obj=account, label="Account")
    ticket_service.add_reference(ticket=ticket, obj=account, label="Account")

    assert SupportTicketReference.objects.filter(ticket=ticket).count() == 1

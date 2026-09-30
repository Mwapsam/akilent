"""Phase B.2: apps.conversations.tasks.escalate_overdue_conversations."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership, Team
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Event, Message, RoutingRule
from apps.conversations.state import OVERDUE_WAITING
from apps.conversations.tasks import escalate_overdue_conversations


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


def _waiting_conversation(account, *, waited, assigned_to=None, assigned_team=None):
    n = Conversation.objects.filter(account=account).count()
    contact = Contact.objects.create(account=account, phone=f"+2609710000{n:02d}")
    conv = Conversation.objects.create(
        account=account,
        contact=contact,
        channel="whatsapp",
        assigned_to=assigned_to,
        assigned_team=assigned_team,
    )
    Message.objects.create(
        account=account,
        conversation=conv,
        direction="inbound",
        body="Still there?",
        timestamp=timezone.now() - waited,
    )
    return conv


@pytest.mark.django_db
def test_an_overdue_unassigned_conversation_is_escalated_and_re_routed(account):
    agent = User.objects.create_user("agent", "a@example.com", "pw")
    Membership.objects.create(user=agent, account=account, role=Membership.Role.MEMBER)
    team = Team.objects.create(account=account, name="Support")
    team.members.add(agent)
    RoutingRule.objects.create(account=account, name="Default", team=team)

    conv = _waiting_conversation(account, waited=OVERDUE_WAITING + timedelta(minutes=1))
    result = escalate_overdue_conversations()
    conv.refresh_from_db()

    assert result == {"escalated": 1}
    assert conv.assigned_team_id == team.id
    assert conv.assigned_to_id == agent.id
    assert Event.objects.filter(
        subject_type="conversation", type="conversation.escalated"
    ).exists()


@pytest.mark.django_db
def test_an_overdue_assigned_conversation_is_escalated_without_being_reassigned(
    account,
):
    agent = User.objects.create_user("agent", "a@example.com", "pw")
    Membership.objects.create(user=agent, account=account, role=Membership.Role.MEMBER)
    conv = _waiting_conversation(
        account, waited=OVERDUE_WAITING + timedelta(minutes=1), assigned_to=agent
    )
    result = escalate_overdue_conversations()
    conv.refresh_from_db()

    assert result == {"escalated": 1}
    assert conv.assigned_to_id == agent.id  # unchanged
    assert Event.objects.filter(
        subject_type="conversation", type="conversation.escalated"
    ).exists()


@pytest.mark.django_db
def test_a_conversation_within_the_overdue_window_is_not_escalated(account):
    _waiting_conversation(account, waited=timedelta(minutes=30))
    result = escalate_overdue_conversations()
    assert result == {"escalated": 0}
    assert not Event.objects.filter(type="conversation.escalated").exists()


@pytest.mark.django_db
def test_a_replied_to_conversation_is_not_escalated(account):
    conv = _waiting_conversation(account, waited=timedelta(hours=5))
    Message.objects.create(
        account=account,
        conversation=conv,
        direction="outbound",
        status="delivered",
        body="Yes, still here",
        timestamp=timezone.now() - timedelta(hours=4),
    )
    result = escalate_overdue_conversations()
    assert result == {"escalated": 0}


@pytest.mark.django_db
def test_the_same_waiting_episode_is_only_escalated_once(account):
    conv = _waiting_conversation(account, waited=OVERDUE_WAITING + timedelta(minutes=1))
    escalate_overdue_conversations()
    escalate_overdue_conversations()
    assert (
        Event.objects.filter(
            subject_type="conversation",
            subject_id=str(conv.pk),
            type="conversation.escalated",
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_a_missed_closed_conversation_is_not_escalated(account):
    """Missed (24h+) conversations are CLOSED, not OPEN — preserved history,
    not silently re-escalated as though newly overdue."""
    conv = _waiting_conversation(account, waited=timedelta(days=2))
    conv.status = Conversation.Status.CLOSED
    conv.save(update_fields=["status"])
    result = escalate_overdue_conversations()
    assert result == {"escalated": 0}

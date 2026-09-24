"""Follow-up completion and per-assignee team performance."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, FollowUp, Message
from apps.conversations.performance import followup_completion, team_performance

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


def followup(account, *, due_days_ago, done=False):
    contact = Contact.objects.create(account=account, phone=f"+26097{FollowUp.objects.count():07d}")
    return FollowUp.objects.create(
        account=account, contact=contact, due_at=NOW - timedelta(days=due_days_ago),
        done_at=NOW if done else None,
    )


def user_in(account, name):
    user = User.objects.create_user(name, f"{name}@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    return user


def convo(account, assignee, *messages):
    contact = Contact.objects.create(account=account, phone=f"+26097{Conversation.objects.count() + 5000000}")
    c = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp", assigned_to=assignee,
        last_message_at=NOW - timedelta(minutes=min(m[1] for m in messages)),
    )
    for direction, minutes_ago, status in messages:
        Message.objects.create(
            account=account, conversation=c, direction=direction, body="x", status=status,
            timestamp=NOW - timedelta(minutes=minutes_ago))
    return c


@pytest.mark.django_db
def test_followup_completion_counts_only_those_that_fell_due_in_the_window(account):
    followup(account, due_days_ago=2, done=True)
    followup(account, due_days_ago=3, done=False)
    followup(account, due_days_ago=45, done=False)   # outside 30 days: not in the rate...
    result = followup_completion(account, now=NOW)
    assert (result["due"], result["completed"], result["completion_pct"]) == (2, 1, 50)
    assert result["overdue"] == 2                    # ...but still overdue


@pytest.mark.django_db
def test_followup_not_yet_due_is_neither_counted_nor_overdue(account):
    followup(account, due_days_ago=-2)
    result = followup_completion(account, now=NOW)
    assert result["due"] == 0 and result["overdue"] == 0


@pytest.mark.django_db
def test_followup_rate_is_none_not_zero_when_nothing_was_due(account):
    assert followup_completion(account, now=NOW)["completion_pct"] is None


@pytest.mark.django_db
def test_followup_completion_is_scoped_to_the_account(account):
    followup(Account.objects.create(company_name="Other"), due_days_ago=2)
    assert followup_completion(account, now=NOW)["due"] == 0


@pytest.mark.django_db
def test_team_performance_per_assignee(account):
    ada, sam = user_in(account, "ada"), user_in(account, "sam")
    convo(account, ada, (IN, 120, "delivered"), (OUT, 100, "delivered"))    # answered in 20 min
    convo(account, ada, (IN, 60, "delivered"), (OUT, 30, "delivered"))      # answered in 30 min
    convo(account, sam, (IN, 45, "delivered"))                              # customer waiting
    convo(account, None, (IN, 45, "delivered"))                             # unassigned: not counted

    rows = {r["name"]: r for r in team_performance(account, now=NOW)}
    assert set(rows) == {"ada", "sam"}
    assert (rows["ada"]["conversations"], rows["ada"]["waiting"], rows["ada"]["avg_first_reply_minutes"]) == (2, 0, 25)
    assert (rows["sam"]["conversations"], rows["sam"]["waiting"], rows["sam"]["avg_first_reply_minutes"]) == (1, 1, None)


@pytest.mark.django_db
def test_team_rows_are_ordered_by_customers_waiting(account):
    ada, sam = user_in(account, "ada"), user_in(account, "sam")
    convo(account, ada, (IN, 60, "delivered"), (OUT, 30, "delivered"))
    convo(account, sam, (IN, 45, "delivered"))
    assert [r["name"] for r in team_performance(account, now=NOW)] == ["sam", "ada"]


@pytest.mark.django_db
def test_team_performance_ignores_conversations_outside_the_window_and_other_accounts(account):
    ada = user_in(account, "ada")
    old = convo(account, ada, (IN, 60 * 24 * 40, "delivered"))
    other = Account.objects.create(company_name="Other")
    convo(other, user_in(other, "eve"), (IN, 45, "delivered"))
    assert old.last_message_at < NOW - timedelta(days=30)
    assert team_performance(account, now=NOW) == []


@pytest.mark.django_db
def test_insights_page_shows_followup_and_team_sections(client, account):
    owner = user_in(account, "owner")
    client.force_login(owner)
    followup(account, due_days_ago=2, done=True)
    convo(account, owner, (IN, 45, "delivered"))
    body = client.get("/email/insights/").content.decode()
    assert "of follow-ups completed" in body
    assert "Team member" in body and "owner" in body

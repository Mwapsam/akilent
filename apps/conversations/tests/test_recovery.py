"""Missed-conversation recovery: one internal follow-up per unanswered WhatsApp enquiry."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, FollowUp, Message
from apps.conversations.recovery import create_missed_followups

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()
HOUR = 60


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


def convo(account, *messages, status=Conversation.Status.OPEN, channel="whatsapp"):
    n = Conversation.objects.count()
    contact = Contact.objects.create(account=account, phone=f"+26097{3000000 + n}")
    c = Conversation.objects.create(account=account, contact=contact, channel=channel, status=status)
    for direction, minutes_ago, st in messages:
        Message.objects.create(
            account=account, conversation=c, direction=direction, body="x",
            timestamp=NOW - timedelta(minutes=minutes_ago), status=st)
    return c


@pytest.mark.django_db
def test_unanswered_for_over_24h_gets_one_due_followup(account):
    c = convo(account, (IN, 30 * HOUR, "delivered"))
    assert create_missed_followups(NOW) == 1
    f = FollowUp.objects.get()
    assert (f.account, f.contact, f.conversation) == (account, c.contact, c)
    assert f.done_at is None and f.due_at == NOW


@pytest.mark.django_db
def test_second_run_does_not_duplicate(account):
    convo(account, (IN, 30 * HOUR, "delivered"))
    create_missed_followups(NOW)
    assert create_missed_followups(NOW) == 0
    assert FollowUp.objects.count() == 1


@pytest.mark.django_db
def test_completed_followup_is_not_recreated_for_the_same_message(account):
    convo(account, (IN, 30 * HOUR, "delivered"))
    create_missed_followups(NOW)
    FollowUp.objects.get().mark_done()
    assert create_missed_followups(NOW) == 0


@pytest.mark.django_db
def test_customer_writing_again_and_being_missed_again_is_reminded_again(account):
    c = convo(account, (IN, 60 * HOUR, "delivered"))
    create_missed_followups(NOW - timedelta(hours=30))
    FollowUp.objects.update(created_at=NOW - timedelta(hours=30))  # auto_now_add uses real time
    FollowUp.objects.get().mark_done()
    Message.objects.create(
        account=account, conversation=c, direction=IN, body="hello?",
        timestamp=NOW - timedelta(hours=25), status="delivered")
    assert create_missed_followups(NOW) == 1


@pytest.mark.django_db
@pytest.mark.parametrize("messages", [
    [(IN, 5 * HOUR, "delivered")],                                   # not missed yet (<24h)
    [(IN, 30 * HOUR, "delivered"), (OUT, 29 * HOUR, "delivered")],   # answered
    [(IN, 30 * HOUR, "delivered"), (OUT, 29 * HOUR, "failed")],      # failed reply is not a response
    [(IN, 30 * 24 * HOUR, "delivered")],                             # older than the 7-day window
])
def test_only_recent_unanswered_conversations_are_reminded(account, messages):
    convo(account, *messages)
    expected = 1 if messages == [(IN, 30 * HOUR, "delivered"), (OUT, 29 * HOUR, "failed")] else 0
    assert create_missed_followups(NOW) == expected


@pytest.mark.django_db
def test_closed_and_non_whatsapp_conversations_are_ignored(account):
    convo(account, (IN, 30 * HOUR, "delivered"), status=Conversation.Status.CLOSED)
    convo(account, (IN, 30 * HOUR, "delivered"), channel="email")
    assert create_missed_followups(NOW) == 0

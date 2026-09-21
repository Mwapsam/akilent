"""Phase 1: derived conversation state - who spoke last, who is waiting, for how long."""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.conversations.state import (
    ConversationState as S,
    calculate_response_time,
    get_conversation_state,
    missed,
    needs_attention,
    snapshot_of,
    with_activity,
)

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()
DAY = 24 * 60


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


def convo(account, *messages, status=Conversation.Status.OPEN, phone=None):
    """messages: (direction, minutes_ago, status)"""
    n = Conversation.objects.filter(account=account).count()
    contact = Contact.objects.create(account=account, phone=phone or f"+26097{2000000 + n}")
    c = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp", status=status)
    for direction, minutes_ago, st in messages:
        Message.objects.create(
            account=account, conversation=c, direction=direction, body="x",
            timestamp=NOW - timedelta(minutes=minutes_ago), status=st)
    return c


def state(c):
    return get_conversation_state(c, now=NOW)


@pytest.mark.django_db
def test_customer_spoke_last_means_waiting_for_agent_with_age(account):
    s = state(convo(account, (IN, 30, "delivered")))
    assert s.state is S.WAITING_FOR_AGENT and s.needs_attention
    assert s.last_speaker == "customer"
    assert s.waiting_age == timedelta(minutes=30)


@pytest.mark.django_db
def test_business_spoke_last_means_waiting_for_customer(account):
    s = state(convo(account, (IN, 30, "delivered"), (OUT, 10, "delivered")))
    assert s.state is S.WAITING_FOR_CUSTOMER and not s.needs_attention
    assert s.last_speaker == "business" and s.waiting_age is None


@pytest.mark.django_db
def test_new_customer_message_after_a_reply_waits_again_measured_from_that_message(account):
    s = state(convo(account, (IN, 60, "delivered"), (OUT, 50, "read"), (IN, 5, "delivered")))
    assert s.state is S.WAITING_FOR_AGENT
    assert s.waiting_age == timedelta(minutes=5)


@pytest.mark.django_db
@pytest.mark.parametrize("bad_status", ["failed", "queued"])
def test_failed_or_queued_reply_is_not_a_response(account, bad_status):
    s = state(convo(account, (IN, 30, "delivered"), (OUT, 10, bad_status)))
    assert s.state is S.WAITING_FOR_AGENT                      # customer still waiting
    assert s.last_activity_at == NOW - timedelta(minutes=10)   # the attempt is still recent activity


@pytest.mark.django_db
def test_exact_tie_counts_as_responded(account):
    s = state(convo(account, (IN, 10, "delivered"), (OUT, 10, "sent")))
    assert s.state is S.WAITING_FOR_CUSTOMER


@pytest.mark.django_db
def test_explicit_close_is_closed_and_a_new_customer_message_makes_it_waiting_again(account):
    c = convo(account, (IN, 30, "delivered"), status=Conversation.Status.CLOSED)
    s = state(c)
    assert s.state is S.CLOSED and s.closed_reason == "closed" and not s.needs_attention
    Message.objects.create(account=account, conversation=c, direction=IN, body="hi",
                           timestamp=NOW - timedelta(minutes=1), status="delivered")
    c.register_inbound(NOW - timedelta(minutes=1))      # what the inbound projection does
    s = state(c)
    assert s.state is S.WAITING_FOR_AGENT and s.waiting_age == timedelta(minutes=1)


@pytest.mark.django_db
def test_inactive_24h_is_closed_and_outbound_never_resets_the_clock(account):
    s = state(convo(account, (IN, DAY + 5, "delivered"), (OUT, 1, "delivered")))  # template just sent
    assert s.state is S.CLOSED and s.closed_reason == "inactive_24h"
    assert state(convo(account, (IN, DAY, "delivered"))).state is S.CLOSED               # exactly 24h: inactive
    assert state(convo(account, (IN, DAY - 1, "delivered"))).state is S.WAITING_FOR_AGENT


@pytest.mark.django_db
def test_missed_flags_unanswered_open_conversations_past_24h(account):
    unanswered = state(convo(account, (IN, DAY + 60, "delivered")))
    assert unanswered.state is S.CLOSED and unanswered.missed
    answered = state(convo(account, (IN, DAY + 60, "delivered"), (OUT, DAY + 50, "delivered")))
    assert not answered.missed
    deliberately_closed = state(convo(account, (IN, DAY + 60, "delivered"), status=Conversation.Status.CLOSED))
    assert not deliberately_closed.missed


@pytest.mark.django_db
def test_no_messages_or_only_unsent_outbound_is_indeterminate(account):
    assert state(convo(account)).state is S.INDETERMINATE
    only_queued = state(convo(account, (OUT, 5, "queued")))
    assert only_queued.state is S.INDETERMINATE and only_queued.last_speaker is None


@pytest.mark.django_db
def test_business_initiated_conversation_waits_for_the_customer(account):
    assert state(convo(account, (OUT, 5, "delivered"))).state is S.WAITING_FOR_CUSTOMER


@pytest.mark.django_db
def test_first_response_time(account):
    c = convo(account, (IN, 60, "delivered"), (OUT, 55, "delivered"), (OUT, 40, "read"), (IN, 30, "delivered"))
    assert calculate_response_time(c) == 5 * 60
    assert state(c).first_response_seconds == 5 * 60
    assert calculate_response_time(convo(account, (IN, 60, "delivered"))) is None      # not invented
    assert calculate_response_time(convo(account, (IN, 60, "delivered"), (OUT, 50, "failed"))) is None
    assert calculate_response_time(convo(account, (OUT, 60, "delivered"))) is None     # no customer message


@pytest.mark.django_db
def test_query_and_function_agree_for_every_scenario(account):
    """needs_attention()/missed() apply the same rules as get_conversation_state()."""
    scenarios = [
        [(IN, 30, "delivered")], [(IN, 30, "delivered"), (OUT, 10, "delivered")],
        [(IN, 30, "delivered"), (OUT, 10, "failed")], [(IN, 10, "delivered"), (OUT, 10, "sent")],
        [(IN, DAY + 5, "delivered")], [(IN, DAY + 5, "delivered"), (OUT, 1, "read")],
        [(IN, 60, "delivered"), (OUT, 50, "read"), (IN, 5, "delivered")], [(OUT, 5, "delivered")], [],
    ]
    for msgs in scenarios:
        convo(account, *msgs)
    convo(account, (IN, 30, "delivered"), status=Conversation.Status.CLOSED)

    by_id = {c.id: get_conversation_state(c, now=NOW) for c in Conversation.objects.filter(account=account)}
    want_attention = {i for i, s in by_id.items() if s.needs_attention}
    want_missed = {i for i, s in by_id.items() if s.missed}
    assert want_attention and want_missed
    assert {c.id for c in needs_attention(account, now=NOW)} == want_attention
    assert {c.id for c in missed(account, now=NOW)} == want_missed
    for c in with_activity(Conversation.objects.filter(account=account)):   # annotated path == per-conversation path
        assert snapshot_of(c, now=NOW).state == by_id[c.id].state


@pytest.mark.django_db
def test_needs_attention_is_account_scoped_and_longest_waiting_first(account):
    other = Account.objects.create(company_name="Other")
    convo(other, (IN, 5, "delivered"), phone="+260970000001")
    newer = convo(account, (IN, 5, "delivered"))
    older = convo(account, (IN, 50, "delivered"))
    assert [c.id for c in needs_attention(account, now=NOW)] == [older.id, newer.id]


@pytest.mark.django_db
def test_needs_attention_and_snapshots_cost_one_query(account, django_assert_num_queries):
    for _ in range(3):
        convo(account, (IN, 5, "delivered"))
    with django_assert_num_queries(1):
        for c in needs_attention(account, now=NOW):
            snapshot_of(c, now=NOW)

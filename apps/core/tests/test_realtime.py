"""apps.core.realtime: the publish half of real-time updates.

Test settings (automator/settings_test.py) set REDIS_URL="", so every test here exercises the
same no-Redis path production runs before REALTIME_SSE_ENABLED is turned on — publish() must
never raise just because there's nothing listening.
"""

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.core.realtime import on_conversation_message_processed, publish


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Realtime Co")


@pytest.fixture
def conversation(account):
    contact = Contact.objects.create(account=account, phone="+260970000099")
    return Conversation.objects.create(
        account=account,
        contact=contact,
        channel=Conversation.Channel.WHATSAPP,
    )


@pytest.mark.django_db
def test_publish_is_a_noop_without_redis(django_capture_on_commit_callbacks):
    """No REDIS_URL (as in tests) must never raise — the caller shouldn't have to check first."""
    with django_capture_on_commit_callbacks(execute=True):
        publish(1, "conversation.updated", {"conversation_id": "abc"})
    # Reaching here without an exception is the assertion; there is nothing to read back.


@pytest.mark.django_db
def test_publish_defers_to_after_commit(django_capture_on_commit_callbacks):
    """publish() must not touch Redis before the transaction it's called in actually commits —
    a subscriber refetching mid-transaction could see a conversation that isn't there yet."""
    sent = []
    with django_capture_on_commit_callbacks() as callbacks:
        publish(1, "conversation.updated", {"conversation_id": "abc"})
        assert not sent  # nothing has run yet — still inside the "transaction"
    for cb in callbacks:
        cb()
        sent.append(True)
    assert (
        sent
    )  # the deferred send does run once actually invoked, and still doesn't raise


@pytest.mark.django_db
def test_message_processed_publishes_conversation_updated_for_any_direction(
    account, conversation
):
    published = []
    import apps.core.realtime as realtime_module

    def fake_publish(account_id, event, ids=None):
        published.append((account_id, event, ids))

    original = realtime_module.publish
    realtime_module.publish = fake_publish
    try:
        for direction in (Message.Direction.INBOUND, Message.Direction.OUTBOUND):
            message = Message.objects.create(
                account=account,
                conversation=conversation,
                direction=direction,
                body="hi",
                timestamp=timezone.now(),
            )
            on_conversation_message_processed(
                sender=Conversation,
                conversation=conversation,
                message=message,
                handled_by_automation=False,
            )
    finally:
        realtime_module.publish = original

    events = [p[1] for p in published]
    assert (
        events.count("conversation.updated") == 2
    )  # once per message, either direction
    assert events.count("message.created") == 1  # only for the inbound one
    assert all(p[0] == account.id for p in published)

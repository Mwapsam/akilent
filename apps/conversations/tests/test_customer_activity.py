"""What an inbound message does beyond landing in the inbox.

Two things a WhatsApp-first business depends on and neither of which existed:
the message shows up on the customer's own history, and the customer counts as
recently engaged — which is what any "hasn't been in touch for N days" rule
reads. And a reminder to chase someone is closed when they write back.
"""
import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations.models import FollowUp
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen", slug="mk2")


def _inbound(account, body, message_id, contact=None):
    if contact is None:
        contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.filter(
        account=account, phone_number="+260971234567"
    ).first() or WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    log = MessageLog.objects.create(
        account=account, conversation=wa_conversation, contact=wa_contact,
        message_id=message_id, direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT, content=body,
        status=MessageLog.Status.DELIVERED, timestamp=timezone.now(),
    )
    conversation = record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=log,
    )
    return conversation, contact


@pytest.mark.django_db
def test_an_inbound_message_lands_on_the_customer_history(account):
    _, contact = _inbound(account, "Are you open on Sunday?", "wamid.ACT1")
    event = contact.events.get(type="conversation.message_received")
    assert "Sunday" in event.data["body"]


@pytest.mark.django_db
def test_an_inbound_message_counts_as_being_in_touch(account):
    """`last_engaged_at` is what the last_engaged_days segment reads. Before
    this it only moved on an email open, so a WhatsApp customer who messaged
    daily still looked untouched for months."""
    _, contact = _inbound(account, "Hello", "wamid.ACT2")
    contact.refresh_from_db()
    assert contact.last_engaged_at is not None


@pytest.mark.django_db
def test_a_reply_closes_the_reminder_to_chase_them(account):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    conversation, _ = _inbound(account, "Hello", "wamid.ACT3", contact=contact)
    followup = FollowUp.objects.create(
        account=account, contact=contact, conversation=conversation,
        due_at=timezone.now() + timezone.timedelta(days=1), note="Chase the quote",
    )

    _inbound(account, "Any update?", "wamid.ACT4", contact=contact)

    followup.refresh_from_db()
    assert followup.done_at is not None


@pytest.mark.django_db
def test_a_reply_does_not_reopen_a_reminder_already_done(account):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    conversation, _ = _inbound(account, "Hello", "wamid.ACT5", contact=contact)
    followup = FollowUp.objects.create(
        account=account, contact=contact, conversation=conversation,
        due_at=timezone.now(), note="Already handled",
    )
    followup.mark_done()
    done_at = followup.done_at

    _inbound(account, "Thanks!", "wamid.ACT6", contact=contact)

    followup.refresh_from_db()
    assert followup.done_at == done_at

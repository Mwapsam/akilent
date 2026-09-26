"""A lead opens itself when a customer asks to buy something.

The architectural intent is Conversation -> Contact -> Lead: the opportunity
originates from what the customer said, not from an agent remembering to press
a button.
"""
import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations.intent import detect_buying_intent
from apps.conversations.services import record_inbound_whatsapp_message
from apps.crm.models import Lead
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact


@pytest.mark.parametrize("body", [
    "How much for the blue dress?",
    "Do you have it in stock?",
    "I want to buy 20 bags",
    "Can I order 3 of these",
    "whats the price",
    "Do you deliver to Kitwe?",
])
def test_buying_intent_is_detected(body):
    assert detect_buying_intent(body) is not None


@pytest.mark.parametrize("body", [
    "Thanks!",
    "Good morning",
    "I called in order to confirm my appointment",
    "Just looking for now",
    "",
])
def test_small_talk_is_not_buying_intent(body):
    assert detect_buying_intent(body) is None


@pytest.fixture
def inbound(db):
    account = Account.objects.create(company_name="Mwamba Kitchen", slug="mk")
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)

    def _receive(body: str, message_id: str):
        log = MessageLog.objects.create(
            account=account, conversation=wa_conversation, contact=wa_contact,
            message_id=message_id, direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.TEXT, content=body,
            status=MessageLog.Status.DELIVERED, timestamp=timezone.now(),
        )
        return record_inbound_whatsapp_message(
            contact=contact, wa_contact=wa_contact,
            whatsapp_conversation=wa_conversation, message_log=log,
        )

    return account, contact, _receive


@pytest.mark.django_db
def test_buying_question_opens_a_lead(inbound):
    account, contact, receive = inbound
    receive("How much for the blue dress?", "wamid.1")

    lead = Lead.objects.get(account=account, contact=contact)
    assert lead.source == "conversation"
    # ...and the customer's timeline says why it appeared.
    event = contact.events.filter(type="lead.auto_created").first()
    assert event is not None
    assert event.data["signal"] == "how much"


@pytest.mark.django_db
def test_small_talk_does_not_open_a_lead(inbound):
    account, contact, receive = inbound
    receive("Good morning", "wamid.1")
    assert Lead.objects.filter(account=account).count() == 0


@pytest.mark.django_db
def test_several_questions_stay_one_opportunity(inbound):
    account, contact, receive = inbound
    receive("How much for the blue dress?", "wamid.1")
    receive("Do you deliver?", "wamid.2")
    receive("Can I order two?", "wamid.3")

    assert Lead.objects.filter(account=account, contact=contact).count() == 1


@pytest.mark.django_db
def test_a_new_question_after_a_closed_lead_opens_another(inbound):
    account, contact, receive = inbound
    receive("How much for the blue dress?", "wamid.1")
    Lead.objects.filter(account=account).update(status=Lead.Status.LOST)

    receive("How much for the red one?", "wamid.2")
    assert Lead.objects.filter(account=account, contact=contact).count() == 2


@pytest.mark.django_db
def test_disabled_sales_module_opens_no_lead(inbound):
    from apps.billing import api as billing_api

    account, contact, receive = inbound
    billing_api.set_owner_switch(account, "sales", False)

    receive("How much for the blue dress?", "wamid.1")
    assert Lead.objects.filter(account=account).count() == 0


@pytest.mark.django_db
def test_the_message_still_lands_if_lead_capture_fails(inbound):
    from unittest.mock import patch

    account, contact, receive = inbound
    with patch("apps.conversations.services.run_action", side_effect=RuntimeError("boom")):
        conversation = receive("How much for the blue dress?", "wamid.1")

    assert conversation is not None
    assert conversation.messages.count() == 1

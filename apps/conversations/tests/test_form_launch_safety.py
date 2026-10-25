"""A form launch that fails must leave nothing behind, and Flows only run on WhatsApp."""

from unittest.mock import patch

import pytest

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations import forms as conversation_forms
from apps.conversations.models import Conversation, ConversationForm, FormResponse
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import WhatsAppContact

QUESTIONS = [
    {"key": "name", "label": "Your name?", "field_type": "text", "maps_to": ""},
]


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Flow Test Co")


@pytest.fixture
def flow_form(account):
    return ConversationForm.objects.create(
        account=account,
        name="Intake",
        status=ConversationForm.Status.PUBLISHED,
        presentation=ConversationForm.Presentation.WHATSAPP_FLOW,
        flow_id="flow_abc",
        flow_status=ConversationForm.FlowStatus.PUBLISHED,
        questions=QUESTIONS,
    )


@pytest.fixture
def wa_conversation(account):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number=contact.phone, contact=contact
    )
    return Conversation.get_or_create_for_whatsapp(
        WhatsAppConversation.get_or_open(wa_contact)
    )


def test_a_failed_flow_launch_leaves_no_form_in_progress(flow_form, wa_conversation):
    with patch(
        "apps.whatsapp.api.send_interactive", side_effect=RuntimeError("meta down")
    ):
        with pytest.raises(RuntimeError):
            conversation_forms.start_form(wa_conversation, flow_form)
    assert not FormResponse.objects.exists()


def test_text_typed_during_a_flow_is_not_taken_as_an_answer(flow_form, wa_conversation):
    with patch("apps.whatsapp.api.send_interactive"):
        conversation_forms.start_form(wa_conversation, flow_form)
    assert conversation_forms.record_answer(wa_conversation, "hello?") is False


def test_a_flow_form_asks_by_text_off_whatsapp(account, flow_form):
    contact = Contact.objects.create(account=account, phone="+260970000000")
    ig = Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.INSTAGRAM
    )
    sent = []
    with patch(
        "apps.conversations.forms._send",
        side_effect=lambda convo, text, **kw: sent.append(text),
    ):
        response = conversation_forms.start_form(ig, flow_form)
    assert sent and response.flow_token == ""
    assert conversation_forms.can_start(flow_form, ig)


def test_unpublished_flow_is_not_offered_on_whatsapp(flow_form, wa_conversation):
    flow_form.flow_status = ConversationForm.FlowStatus.NEEDS_REPUBLISH
    assert not conversation_forms.can_start(flow_form, wa_conversation)

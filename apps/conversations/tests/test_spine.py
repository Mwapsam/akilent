from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow
from apps.automation.workflow_engine import enroll_for_trigger
from apps.contacts.models import Contact
from apps.conversations.actions import ActionError, run_action
from apps.conversations.models import Conversation, ConversationNote, Event, Message
from apps.conversations.services import emit_event, record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971234567")


@pytest.fixture
def wa_contact(account, contact):
    return WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact,
    )


@pytest.fixture
def wa_conversation(wa_contact):
    return WhatsAppConversation.get_or_open(wa_contact)


@pytest.fixture
def message_log(account, wa_contact, wa_conversation):
    return MessageLog.objects.create(
        account=account, conversation=wa_conversation, contact=wa_contact,
        message_id="wamid.TEST123", direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT, content="Do you have the blue dress?",
        status=MessageLog.Status.DELIVERED, timestamp=timezone.now(),
    )


@pytest.mark.django_db
def test_record_inbound_message_creates_generic_conversation_and_message(
    contact, wa_contact, wa_conversation, message_log,
):
    conversation = record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )

    assert conversation is not None
    assert conversation.contact_id == contact.id
    assert conversation.channel == Conversation.Channel.WHATSAPP
    assert conversation.whatsapp_conversation_id == wa_conversation.id
    assert conversation.is_unread is True

    message = Message.objects.get(whatsapp_message=message_log)
    assert message.conversation_id == conversation.id
    assert message.body == "Do you have the blue dress?"
    assert message.direction == Message.Direction.INBOUND


@pytest.mark.django_db
def test_record_inbound_message_emits_immutable_event(
    contact, wa_contact, wa_conversation, message_log,
):
    record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )

    event = Event.objects.get(type="conversation.message_received")
    assert event.source == "whatsapp"
    assert event.source_event_id == "wamid.TEST123"
    assert event.subject_type == "conversation"


@pytest.mark.django_db
def test_record_inbound_message_is_idempotent_on_replay(
    contact, wa_contact, wa_conversation, message_log,
):
    record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )
    # Simulate a replayed webhook re-processing the same MessageLog.
    second = record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )

    assert second is None
    assert Message.objects.filter(whatsapp_message=message_log).count() == 1
    assert Event.objects.filter(type="conversation.message_received").count() == 1


@pytest.mark.django_db
def test_emit_event_deduplicates_on_account_source_source_event_id(account):
    kwargs = dict(
        account=account, type="order.paid", occurred_at=timezone.now(),
        source="commerce", source_event_id="order-1",
    )
    first = emit_event(**kwargs)
    second = emit_event(**kwargs)

    assert first is not None
    assert second is None
    assert Event.objects.filter(type="order.paid").count() == 1


@pytest.mark.django_db
def test_message_received_enrolls_published_workflow(
    account, contact, wa_contact, wa_conversation, message_log,
):
    Workflow.objects.create(
        account=account, name="Reply to enquiries", slug="reply-to-enquiries",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "conversation.message_received"},
            "steps": [{"id": "stop", "type": "stop"}],
        },
    )

    record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )

    from apps.automation.models import WorkflowRun

    assert WorkflowRun.objects.filter(contact=contact).exists()


@pytest.mark.django_db
def test_enroll_for_trigger_matches_new_trigger_type(account, contact):
    wf = Workflow.objects.create(
        account=account, name="Spine trigger", slug="spine-trigger",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "conversation.message_received"},
            "steps": [{"id": "stop", "type": "stop"}],
        },
    )
    n = enroll_for_trigger(account.id, "conversation.message_received", contact)
    assert n == 1


@pytest.mark.django_db
def test_action_registry_assign_conversation(
    contact, wa_contact, wa_conversation, message_log,
):
    from django.contrib.auth import get_user_model

    conversation = record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )
    user = get_user_model().objects.create(username="mwape", email="mwape@example.com")
    Membership.objects.create(user=user, account=conversation.account)

    result = run_action("assign_conversation", {}, conversation=conversation, user=user)

    conversation.refresh_from_db()
    assert conversation.assigned_to_id == user.id
    assert result["assigned_to_id"] == user.id


@pytest.mark.django_db
def test_action_registry_add_internal_note(
    contact, wa_contact, wa_conversation, message_log,
):
    conversation = record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )

    run_action("add_internal_note", {}, conversation=conversation, body="Called back")

    assert ConversationNote.objects.filter(conversation=conversation, body="Called back").exists()


@pytest.mark.django_db
def test_action_registry_rejects_unknown_action():
    with pytest.raises(ActionError):
        run_action("delete_universe", {})

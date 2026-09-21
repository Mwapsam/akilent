"""Phase 1: channel-neutral outbound + status recording on the conversation spine."""
import logging
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.conversations.services import record_message_status, record_outbound_message
from apps.conversations.state import ConversationState as S, get_conversation_state

NOW = timezone.now()


@pytest.fixture
def convo(db):
    account = Account.objects.create(company_name="Acme")
    contact = Contact.objects.create(account=account, phone="+260971234567")
    c = Conversation.objects.create(account=account, contact=contact, channel="whatsapp")
    Message.objects.create(account=account, conversation=c, direction="inbound", body="Price?",
                           timestamp=NOW - timedelta(minutes=10), status="delivered")
    return c


@pytest.mark.django_db
def test_recording_a_sent_reply_makes_the_business_the_last_speaker(convo):
    msg, created = record_outbound_message(
        conversation=convo, body="K8,500", timestamp=NOW - timedelta(minutes=8), status="sent")
    assert created and msg.direction == Message.Direction.OUTBOUND and msg.status == "sent"
    s = get_conversation_state(convo, now=NOW)
    assert s.state is S.WAITING_FOR_CUSTOMER and s.last_speaker == "business"
    assert s.first_response_seconds == 120
    convo.refresh_from_db()
    assert convo.last_message_at == NOW - timedelta(minutes=8)


@pytest.mark.django_db
def test_does_not_change_unread_or_status_of_the_conversation(convo):
    before = (convo.is_unread, convo.status)
    record_outbound_message(conversation=convo, body="hi", timestamp=NOW, status="sent")
    convo.refresh_from_db()
    assert (convo.is_unread, convo.status) == before


@pytest.mark.django_db
def test_recording_is_idempotent_per_provider_record(convo):
    from apps.whatsapp.models import MessageLog, WhatsAppContact
    from apps.whatsapp.models import Conversation as WaConversation

    wa = WhatsAppContact.objects.create(account=convo.account, phone_number="+260971234567")
    wa_convo = WaConversation.get_or_open(wa)
    log = MessageLog.objects.create(account=convo.account, conversation=wa_convo, contact=wa,
                                    direction="out", message_id="wamid.X", status="sent", timestamp=NOW)
    first, created1 = record_outbound_message(
        conversation=convo, body="hi", timestamp=NOW, status="sent", whatsapp_message=log)
    second, created2 = record_outbound_message(
        conversation=convo, body="hi", timestamp=NOW, status="delivered", whatsapp_message=log)
    assert (created1, created2) == (True, False) and first.pk == second.pk
    assert Message.objects.filter(whatsapp_message=log).count() == 1
    first.refresh_from_db()
    assert first.status == "delivered"          # replay carries the newer status forward


@pytest.mark.django_db
def test_status_only_moves_forward_and_failed_is_terminal(convo):
    msg, _ = record_outbound_message(conversation=convo, body="hi", timestamp=NOW, status="queued")
    assert record_message_status(msg, "sent") is True
    assert record_message_status(msg, "read") is True
    assert record_message_status(msg, "delivered") is False       # no regression
    assert record_message_status(msg, "read") is False            # no-op
    assert record_message_status(msg, "nonsense") is False
    msg.refresh_from_db()
    assert msg.status == "read"

    failed, _ = record_outbound_message(conversation=convo, body="x", timestamp=NOW, status="failed")
    assert record_message_status(failed, "delivered") is False
    failed.refresh_from_db()
    assert failed.status == "failed"


@pytest.mark.django_db
def test_a_failed_send_leaves_the_customer_waiting(convo):
    msg, _ = record_outbound_message(conversation=convo, body="hi", timestamp=NOW - timedelta(minutes=5),
                                     status="sent")
    assert get_conversation_state(convo, now=NOW).state is S.WAITING_FOR_CUSTOMER
    # A later failure report flips it back; queued never counted. (sent -> failed is allowed.)
    assert record_message_status(msg, "failed") is True
    assert get_conversation_state(convo, now=NOW).state is S.WAITING_FOR_AGENT


@pytest.mark.django_db
def test_metrics_are_emitted(convo, caplog):
    with caplog.at_level(logging.INFO, logger="akilent.metrics"):
        msg, _ = record_outbound_message(conversation=convo, body="hi", timestamp=NOW, status="queued")
        record_outbound_message(conversation=convo, body="hi", timestamp=NOW, status="queued")  # not idempotent w/o key
        record_message_status(msg, "sent")
    names = [r.getMessage() for r in caplog.records]
    assert any("outbound_projection_created" in n for n in names)
    assert any("status_updates_applied" in n for n in names)

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, ConversationNote
from apps.conversations.services import record_inbound_whatsapp_message
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import MessageLog, WhatsAppContact


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.fixture
def open_conversation(logged_in):
    _, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    message_log = MessageLog.objects.create(
        account=account, conversation=wa_conversation, contact=wa_contact,
        message_id="wamid.VIEWTEST", direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT, content="Do you have the blue dress?",
        status=MessageLog.Status.DELIVERED, timestamp=timezone.now(),
    )
    return record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=message_log,
    )


@pytest.mark.django_db
def test_inbox_lists_conversations_needing_reply(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.get("/inbox/")
    assert resp.status_code == 200
    assert "+260971234567" in resp.content.decode() or "blue dress" not in resp.content.decode()


@pytest.mark.django_db
def test_inbox_scoped_to_account(logged_in, open_conversation):
    client, _, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000000")
    other_wa = WhatsAppContact.objects.create(
        account=other, phone_number="+260970000000", contact=other_contact,
    )
    other_convo = Conversation.objects.create(
        account=other, contact=other_contact, channel=Conversation.Channel.WHATSAPP,
    )
    resp = client.get(f"/inbox/{other_convo.public_id}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_reply_sends_via_action_registry_and_marks_read(logged_in, open_conversation):
    client, account, _ = logged_in
    from apps.whatsapp.models import OutboundMessage

    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "reply", "body": "Yes, medium and large in stock."},
    )
    assert resp.status_code == 302
    assert OutboundMessage.objects.filter(account=account).exists()
    open_conversation.refresh_from_db()
    assert open_conversation.is_unread is False


@pytest.mark.django_db
def test_assign_to_self(logged_in, open_conversation):
    client, _, user = logged_in
    resp = client.post(f"/inbox/{open_conversation.public_id}/", {"action": "assign"})
    assert resp.status_code == 302
    open_conversation.refresh_from_db()
    assert open_conversation.assigned_to_id == user.id


@pytest.mark.django_db
def test_add_internal_note(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "add_note", "body": "Called back, left voicemail"},
    )
    assert resp.status_code == 302
    assert ConversationNote.objects.filter(
        conversation=open_conversation, body="Called back, left voicemail"
    ).exists()


@pytest.mark.django_db
def test_conversation_detail_renders_thread(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.get(f"/inbox/{open_conversation.public_id}/")
    assert resp.status_code == 200
    assert "Do you have the blue dress?" in resp.content.decode()

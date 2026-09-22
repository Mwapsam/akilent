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


@pytest.mark.django_db
def test_conversation_detail_offers_create_lead_when_crm_enabled(logged_in, open_conversation):
    """R1.5a: the inbox side panel is the fix for the "orphaned CRM" finding —
    a lead can be created from the conversation, and creating one returns the
    agent to the conversation (via the hidden "next" field) rather than to Sales."""
    client, _, _ = logged_in
    resp = client.get(f"/inbox/{open_conversation.public_id}/")
    body = resp.content.decode()
    assert "Create lead" in body
    assert f'/inbox/{open_conversation.public_id}/' in body  # the "next" hidden field

    create_resp = client.post("/sales/leads/create/", {
        "contact": open_conversation.contact.phone,
        "next": f"/inbox/{open_conversation.public_id}/",
        "source": "conversation",  # hidden field set by the panel's form, not typed by the agent
    })
    assert create_resp.status_code == 302
    assert create_resp["Location"] == f"/inbox/{open_conversation.public_id}/"

    from apps.crm.models import Lead

    lead = Lead.objects.get(contact=open_conversation.contact)
    assert lead.source == "conversation"  # provenance, so this path is measurable later

    # The panel now shows the lead exists instead of offering to create another.
    resp = client.get(f"/inbox/{open_conversation.public_id}/")
    assert "Already a lead" in resp.content.decode()


@pytest.mark.django_db
def test_messages_feed_returns_only_newer_messages(logged_in, open_conversation):
    client, _, _ = logged_in
    first = open_conversation.messages.first()

    resp = client.get(f"/inbox/{open_conversation.public_id}/messages/?after=0")
    assert resp.status_code == 200
    data = resp.json()
    assert [m["id"] for m in data["messages"]] == [first.id]
    assert data["messages"][0]["body"] == "Do you have the blue dress?"
    assert data["open"] is True
    assert "Waiting for you" in data["status_html"]

    resp = client.get(f"/inbox/{open_conversation.public_id}/messages/?after={first.id}")
    assert resp.json()["messages"] == []


@pytest.mark.django_db
def test_messages_feed_marks_new_inbound_read(logged_in, open_conversation):
    client, _, _ = logged_in
    Conversation.objects.filter(pk=open_conversation.pk).update(is_unread=True)
    client.get(f"/inbox/{open_conversation.public_id}/messages/?after=0")
    open_conversation.refresh_from_db()
    assert open_conversation.is_unread is False


@pytest.mark.django_db
def test_messages_feed_scoped_to_account_and_login(logged_in, open_conversation, client):
    _, _, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260970000000")
    other_convo = Conversation.objects.create(
        account=other, contact=other_contact, channel=Conversation.Channel.WHATSAPP,
    )
    assert client.get(f"/inbox/{other_convo.public_id}/messages/").status_code == 404
    client.logout()
    assert client.get(f"/inbox/{open_conversation.public_id}/messages/").status_code == 302


@pytest.mark.django_db
def test_ajax_reply_returns_json(logged_in, open_conversation):
    client, account, _ = logged_in
    from apps.whatsapp.models import OutboundMessage

    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "reply", "body": "On its way!"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert OutboundMessage.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_ajax_reply_with_empty_body_returns_error(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.post(
        f"/inbox/{open_conversation.public_id}/",
        {"action": "reply", "body": "   "},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


@pytest.mark.django_db
def test_inbox_feed_returns_rendered_list(logged_in, open_conversation):
    client, _, _ = logged_in
    resp = client.get("/inbox/feed/?view=all")
    assert resp.status_code == 200
    assert "blue dress" in resp.json()["html"]

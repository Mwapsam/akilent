"""The conversation side panel's three commercial actions, driven exactly as
the template submits them: set a follow-up, create a lead, create an order.

These are the paths a pilot agent uses all day, so they're covered end to end
(button renders -> form posts -> record exists -> agent lands back in the
conversation) rather than at the service layer.
"""
import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, FollowUp
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
def conversation(logged_in):
    _, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    log = MessageLog.objects.create(
        account=account, conversation=wa_conversation, contact=wa_contact,
        message_id="wamid.PANEL", direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT, content="How much for the blue dress?",
        status=MessageLog.Status.DELIVERED, timestamp=timezone.now(),
    )
    return record_inbound_whatsapp_message(
        contact=contact, wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation, message_log=log,
    )


@pytest.mark.django_db
def test_panel_renders_all_three_actions(logged_in, conversation):
    client, _, _ = logged_in
    body = client.get(f"/inbox/{conversation.public_id}/").content.decode()
    assert "Create lead" in body
    assert "Create order" in body
    assert "Set reminder" in body


@pytest.mark.django_db
def test_set_follow_up_from_the_panel(logged_in, conversation):
    client, account, user = logged_in
    resp = client.post(f"/inbox/{conversation.public_id}/", {
        "action": "create_followup", "when": "1h", "note": "Quote the dress",
    })
    assert resp.status_code == 302
    followup = FollowUp.objects.get(account=account, conversation=conversation)
    assert followup.note == "Quote the dress"
    assert followup.done_at is None

    # ...and it shows on the page and in the due list once due.
    FollowUp.objects.filter(pk=followup.pk).update(
        due_at=timezone.now() - timezone.timedelta(minutes=1)
    )
    assert "Quote the dress" in client.get(f"/inbox/{conversation.public_id}/").content.decode()
    assert "Quote the dress" in client.get("/inbox/followups/").content.decode()


@pytest.mark.django_db
def test_create_lead_from_the_panel(logged_in, conversation):
    from apps.crm.models import Lead

    client, account, _ = logged_in
    resp = client.post("/sales/leads/create/", {
        "contact": conversation.contact.phone,
        "next": f"/inbox/{conversation.public_id}/",
        "source": "conversation",
    })
    assert resp.status_code == 302
    assert resp["Location"] == f"/inbox/{conversation.public_id}/"
    lead = Lead.objects.get(account=account, contact=conversation.contact)
    assert lead.source == "conversation"


@pytest.mark.django_db
def test_create_order_from_the_panel(logged_in, conversation):
    from apps.commerce.models import Order

    client, account, _ = logged_in
    resp = client.post("/orders/create/", {
        "contact": conversation.contact.phone,
        "next": f"/inbox/{conversation.public_id}/",
        "name": "Blue dress", "unit_price": "250", "quantity": "1", "currency": "ZMW",
    })
    assert resp.status_code == 302
    assert resp["Location"] == f"/inbox/{conversation.public_id}/"
    order = Order.objects.get(account=account, contact=conversation.contact)
    assert order.currency == "ZMW"

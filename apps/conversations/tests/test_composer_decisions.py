"""The composer is where the next action is decided.

An owner working through thirty conversations shouldn't have to hunt the side
panel to remember a customer or mark them as interested — the two decisions sit
under the box they're already typing in. Both reuse actions that already exist;
the side panel keeps the record of each.
"""
import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import FollowUp
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


def _conversation(account, body, message_id):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=account, phone_number="+260971234567", contact=contact,
    )
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
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


@pytest.fixture
def conversation(logged_in):
    _, account, _ = logged_in
    return _conversation(account, "Are you open on Sunday?", "wamid.NEXT")


@pytest.mark.django_db
def test_the_composer_offers_both_decisions(logged_in, conversation):
    client, _, _ = logged_in
    body = client.get(f"/inbox/{conversation.public_id}/").content.decode()
    assert "Remind me tomorrow" in body
    assert "Track as interested" in body


@pytest.mark.django_db
def test_remind_me_tomorrow_is_one_click(logged_in, conversation):
    client, account, _ = logged_in
    resp = client.post(f"/inbox/{conversation.public_id}/", {
        "action": "create_followup", "when": "tomorrow",
    })
    assert resp.status_code == 302

    followup = FollowUp.objects.get(account=account, conversation=conversation)
    assert followup.due_at > timezone.now()
    assert followup.done_at is None


@pytest.mark.django_db
def test_the_reminder_replaces_the_button_once_set(logged_in, conversation):
    """No second reminder on offer for a customer already being chased."""
    client, _, _ = logged_in
    client.post(f"/inbox/{conversation.public_id}/", {
        "action": "create_followup", "when": "tomorrow",
    })
    body = client.get(f"/inbox/{conversation.public_id}/").content.decode()
    assert "Reminder set for" in body
    assert "Remind me tomorrow" not in body


@pytest.mark.django_db
def test_an_opportunity_already_captured_is_shown_not_re_offered(logged_in):
    """"How much…" trips deterministic capture, so the decision is already made
    and the row reports it instead of inviting a duplicate."""
    client, account, _ = logged_in
    conversation = _conversation(account, "How much for the blue dress?", "wamid.NEXTBUY")
    body = client.get(f"/inbox/{conversation.public_id}/").content.decode()
    assert "Tracked as interested" in body
    assert "Track as interested" not in body

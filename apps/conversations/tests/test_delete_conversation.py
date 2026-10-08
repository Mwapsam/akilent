"""Owners and admins can delete a conversation from the inbox; everyone else can't."""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from apps.accounts.models import Membership
from apps.conversations.models import Conversation, Event, Message
from apps.conversations.tests import test_views
from apps.crm.models import Lead

# Same setup as the inbox view tests: an owner, and a WhatsApp conversation that already has a lead.
logged_in = test_views.logged_in
open_conversation = test_views.open_conversation


def _delete(client, conversation):
    return client.post(reverse("conversations:delete", args=[conversation.public_id]))


@pytest.mark.django_db
def test_owner_deletes_conversation_and_keeps_the_lead(logged_in, open_conversation):
    client, account, user = logged_in
    lead = Lead.objects.get(conversation=open_conversation)

    response = _delete(client, open_conversation)

    assert response.status_code == 302
    assert response.url == reverse("conversations:inbox")
    assert not Conversation.objects.filter(pk=open_conversation.pk).exists()
    assert not Message.objects.filter(conversation_id=open_conversation.pk).exists()
    lead.refresh_from_db()
    assert lead.conversation_id is None
    event = Event.objects.get(type="conversation.deleted")
    assert event.subject_id == open_conversation.public_id
    assert event.actor == user.username


@pytest.mark.django_db
def test_member_cannot_delete(logged_in, open_conversation):
    client, account, _ = logged_in
    member = User.objects.create_user("m", "m@example.com", "pw")
    Membership.objects.create(user=member, account=account, role=Membership.Role.MEMBER)
    client.force_login(member)

    _delete(client, open_conversation)

    assert Conversation.objects.filter(pk=open_conversation.pk).exists()


@pytest.mark.django_db
def test_delete_needs_a_post(logged_in, open_conversation):
    client, _, _ = logged_in
    url = reverse("conversations:delete", args=[open_conversation.public_id])
    assert client.get(url).status_code == 405
    assert Conversation.objects.filter(pk=open_conversation.pk).exists()


@pytest.mark.django_db
def test_button_shown_to_owner_only(logged_in, open_conversation):
    client, account, _ = logged_in
    url = reverse("conversations:detail", args=[open_conversation.public_id])
    assert b"Delete conversation" in client.get(url).content

    member = User.objects.create_user("m", "m@example.com", "pw")
    Membership.objects.create(user=member, account=account, role=Membership.Role.MEMBER)
    client.force_login(member)
    assert b"Delete conversation" not in client.get(url).content


@pytest.mark.django_db
def test_customer_writing_again_starts_a_new_conversation(logged_in, open_conversation):
    from django.utils import timezone

    from apps.conversations.services import record_inbound_whatsapp_message
    from apps.whatsapp.models import MessageLog, WhatsAppContact

    client, account, _ = logged_in
    _delete(client, open_conversation)

    wa_contact = WhatsAppContact.objects.get(account=account)
    wa_conversation = MessageLog.objects.get(message_id="wamid.VIEWTEST").conversation
    log = MessageLog.objects.create(
        account=account,
        conversation=wa_conversation,
        contact=wa_contact,
        message_id="wamid.AGAIN",
        direction=MessageLog.Direction.INBOUND,
        message_type=MessageLog.MessageType.TEXT,
        content="Hello again",
        status=MessageLog.Status.DELIVERED,
        timestamp=timezone.now(),
    )
    fresh = record_inbound_whatsapp_message(
        contact=wa_contact.contact,
        wa_contact=wa_contact,
        whatsapp_conversation=wa_conversation,
        message_log=log,
    )
    assert fresh is not None and fresh.pk != open_conversation.pk
    assert list(fresh.messages.values_list("body", flat=True)) == ["Hello again"]

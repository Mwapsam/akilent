"""The optional "which conversation led to this?" picker on manual lead and order forms."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.commerce.models import Order
from apps.contacts.models import Contact
from apps.conversations import attribution
from apps.conversations.models import Conversation, ConversationAttribution as CA, Message
from apps.crm.models import Lead

NOW = timezone.now()


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def chat(account, phone, *, days_ago=1, inbound=True):
    contact = Contact.objects.create(account=account, phone=phone, first_name=f"Cust {phone[-3:]}")
    c = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp", last_message_at=NOW - timedelta(days=days_ago))
    Message.objects.create(
        account=account, conversation=c, timestamp=NOW - timedelta(days=days_ago), body="hi",
        direction=Message.Direction.INBOUND if inbound else Message.Direction.OUTBOUND)
    return c


@pytest.mark.django_db
def test_picker_offers_only_conversations_the_customer_wrote_in(logged_in):
    _, account = logged_in
    spoke = chat(account, "+260971000101")
    chat(account, "+260971000102", inbound=False)
    other = Account.objects.create(company_name="Other")
    chat(other, "+260971000103")
    assert [c["public_id"] for c in attribution.recent_choices(account)] == [spoke.public_id]


@pytest.mark.django_db
def test_forms_show_the_picker(logged_in):
    client, account = logged_in
    chat(account, "+260971000104")
    for url in ("/sales/", "/orders/"):
        html = client.get(url).content.decode()
        assert "Which conversation led to this?" in html and "Cust 104" in html


@pytest.mark.django_db
def test_lead_from_the_picker_is_explicit_and_needs_no_contact_text(logged_in):
    client, account = logged_in
    old = chat(account, "+260971000105", days_ago=50)  # too old for the fallback; the pick still counts
    client.post("/sales/leads/create/", {"conversation": old.public_id})
    lead = Lead.objects.get(account=account)
    assert lead.contact == old.contact and lead.conversation == old
    assert lead.attribution.method == CA.Method.EXPLICIT


@pytest.mark.django_db
def test_order_from_the_picker_is_explicit(logged_in):
    client, account = logged_in
    older, newer = chat(account, "+260971000106", days_ago=5), None
    newer = Conversation.objects.create(
        account=account, contact=older.contact, channel="whatsapp", last_message_at=NOW)
    Message.objects.create(account=account, conversation=newer, timestamp=NOW, body="x",
                           direction=Message.Direction.INBOUND)
    client.post("/orders/create/", {
        "contact": older.contact.phone, "conversation": older.public_id,
        "name": "Thing", "unit_price": "10", "quantity": "1"})
    order = Order.objects.get(account=account)
    assert order.conversation == older and order.attribution.method == CA.Method.EXPLICIT


@pytest.mark.django_db
def test_no_pick_keeps_the_recent_fallback(logged_in):
    client, account = logged_in
    c = chat(account, "+260971000107")
    client.post("/sales/leads/create/", {"contact": c.contact.phone})
    assert Lead.objects.get(account=account).attribution.method == CA.Method.RECENT_CONVERSATION


@pytest.mark.django_db
def test_a_conversation_of_a_different_customer_is_refused(logged_in):
    client, account = logged_in
    theirs = chat(account, "+260971000108")
    mine = Contact.objects.create(account=account, phone="+260971000109")
    client.post("/sales/leads/create/", {"contact": mine.phone, "conversation": theirs.public_id})
    assert not Lead.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_another_accounts_conversation_is_refused(logged_in):
    client, account = logged_in
    theirs = chat(Account.objects.create(company_name="Other"), "+260971000110")
    client.post("/sales/leads/create/", {"conversation": theirs.public_id})
    assert not Lead.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_nothing_chosen_and_no_contact_is_an_error(logged_in):
    client, account = logged_in
    client.post("/sales/leads/create/", {})
    assert not Lead.objects.filter(account=account).exists()

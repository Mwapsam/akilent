"""Conversation -> lead -> deal -> order links, the funnel, and revenue by channel."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.commerce.models import Order
from apps.commerce.services import create_order, mark_paid, request_payment  # noqa: F401
from apps.contacts.models import Contact
from apps.conversations.attribution import resolve_conversation
from apps.conversations.models import Conversation, Message
from apps.conversations.api import funnel, revenue_by_channel
from apps.crm.models import Deal, Lead
from apps.crm.services import convert_lead_to_deal, create_lead

NOW = timezone.now()


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


def chat(account, phone="+260971000001", *, days_ago=1, direction=Message.Direction.INBOUND, channel="whatsapp"):
    contact = Contact.objects.filter(account=account, phone=phone).first() or Contact.objects.create(
        account=account, phone=phone)
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel=channel, last_message_at=NOW - timedelta(days=days_ago))
    Message.objects.create(
        account=account, conversation=conversation, direction=direction, body="hi",
        timestamp=NOW - timedelta(days=days_ago))
    return conversation


def item(price="50.00"):
    return [{"name": "Thing", "unit_price": Decimal(price), "quantity": 1}]


def pay(order):
    payment = order.payments.create(account=order.account, amount=order.total, currency=order.currency)
    mark_paid(payment, transaction_id=f"tx{order.pk}")
    return order


@pytest.mark.django_db
def test_resolve_prefers_the_named_conversation(account):
    older = chat(account, days_ago=5)
    chat(account, days_ago=1)
    assert resolve_conversation(account, older.contact, public_id=older.public_id) == older


@pytest.mark.django_db
def test_resolve_falls_back_to_latest_conversation_where_the_customer_spoke(account):
    latest = chat(account, days_ago=2)
    assert resolve_conversation(account, latest.contact) == latest


@pytest.mark.django_db
def test_resolve_ignores_old_or_business_only_conversations(account):
    old = chat(account, "+260971000002", days_ago=45)
    assert resolve_conversation(account, old.contact) is None
    outbound = chat(account, "+260971000003", days_ago=1, direction=Message.Direction.OUTBOUND)
    assert resolve_conversation(account, outbound.contact) is None


@pytest.mark.django_db
def test_resolve_ignores_a_named_conversation_of_another_customer(account):
    other = chat(account, "+260971000004")
    mine = Contact.objects.create(account=account, phone="+260971000005")
    assert resolve_conversation(account, mine, public_id=other.public_id) is None


@pytest.mark.django_db
def test_lead_deal_and_order_carry_the_conversation(account):
    conversation = chat(account)
    lead = create_lead(account, conversation.contact, source="conversation", conversation_id=conversation.public_id)
    assert lead.conversation == conversation
    deal = convert_lead_to_deal(lead, value=100)
    assert deal.conversation == conversation
    order = create_order(account, conversation.contact, item())
    assert order.conversation == conversation


@pytest.mark.django_db
def test_lead_for_a_customer_who_never_wrote_has_no_conversation(account):
    contact = Contact.objects.create(account=account, phone="+260971000009")
    assert create_lead(account, contact).conversation is None


@pytest.mark.django_db
def test_link_is_fixed_at_creation(account):
    first = chat(account, days_ago=3)
    order = create_order(account, first.contact, item())
    chat(account, days_ago=0)  # a newer chat later must not steal the credit
    order.refresh_from_db()
    assert order.conversation_id == first.id


@pytest.mark.django_db
def test_revenue_by_channel_counts_only_paid_orders_and_shows_the_unattributed(account):
    wa = chat(account, "+260971000010")
    em = chat(account, "+260971000011", channel="email")
    stranger = Contact.objects.create(account=account, phone="+260971000012")
    pay(create_order(account, wa.contact, item("50")))
    pay(create_order(account, wa.contact, item("25")))
    pay(create_order(account, em.contact, item("10")))
    pay(create_order(account, stranger, item("7")))
    create_order(account, wa.contact, item("999"))  # unpaid: no revenue

    rows = {r["channel"]: r for r in revenue_by_channel(account, now=NOW + timedelta(minutes=1))}
    assert (rows["whatsapp"]["orders"], rows["whatsapp"]["total"]) == (2, Decimal("75.00"))
    assert rows["email"]["total"] == Decimal("10.00")
    assert rows["none"]["attributed"] is False and rows["none"]["total"] == Decimal("7.00")


@pytest.mark.django_db
def test_currencies_are_not_mixed(account):
    wa = chat(account)
    pay(create_order(account, wa.contact, item("10"), currency="USD"))
    pay(create_order(account, wa.contact, item("10"), currency="ZMW"))
    assert {r["currency"] for r in revenue_by_channel(account, now=NOW + timedelta(minutes=1))} == {"USD", "ZMW"}


@pytest.mark.django_db
def test_other_accounts_revenue_is_invisible(account):
    other = Account.objects.create(company_name="Other")
    theirs = chat(other)
    pay(create_order(other, theirs.contact, item()))
    assert revenue_by_channel(account, now=NOW + timedelta(minutes=1)) == []


@pytest.mark.django_db
def test_funnel_follows_a_customer_from_message_to_payment(account):
    conversation = chat(account)
    lead = create_lead(account, conversation.contact, conversation_id=conversation.public_id)
    deal = convert_lead_to_deal(lead, value=50)
    from apps.crm.services import move_deal_stage
    move_deal_stage(deal, deal.pipeline.stages.get(is_won=True))
    pay(create_order(account, conversation.contact, item()))
    chat(account, "+260971000020")  # wrote in, went nowhere

    result = funnel(account, now=NOW + timedelta(minutes=1))
    assert (result["wrote_in"], result["leads"], result["deals"], result["won"], result["paid"]) == (2, 1, 1, 1, 1)


@pytest.mark.django_db
def test_insights_shows_revenue_by_channel(client, account):
    from django.contrib.auth.models import User

    from apps.accounts.models import Membership

    user = User.objects.create_user("owner", "o@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    wa = chat(account)
    pay(create_order(account, wa.contact, item("42")))
    client.force_login(user)
    response = client.get("/email/insights/")
    assert response.status_code == 200
    assert b"42.00" in response.content

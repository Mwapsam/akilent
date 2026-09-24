"""A payment shows up in the customer's conversation, not just in Orders.

"The conversation stays the customer's story" — the business shouldn't have to
go and look somewhere else to find out the customer paid.
"""
import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.commerce.models import Payment
from apps.commerce.services import create_order, mark_paid, request_payment
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.conversations.state import needs_attention


@pytest.fixture
def paid_setup(db):
    account = Account.objects.create(company_name="Mwamba Kitchen", slug="mk")
    contact = Contact.objects.create(account=account, phone="+260971234567")
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP,
        last_message_at=timezone.now(),
    )
    order = create_order(
        account, contact,
        [{"name": "Blue dress", "unit_price": "250", "quantity": 1}],
        currency="ZMW",
    )
    return account, contact, conversation, order


@pytest.mark.django_db
def test_payment_appears_in_the_conversation(paid_setup):
    account, contact, conversation, order = paid_setup
    payment = request_payment(order, redirect_url="https://example.com/thanks")
    mark_paid(payment, transaction_id="tx_1")

    line = conversation.messages.get(direction=Message.Direction.SYSTEM)
    assert "Payment received" in line.body
    assert "ZMW" in line.body


@pytest.mark.django_db
def test_a_system_line_does_not_change_who_is_waiting(paid_setup):
    account, contact, conversation, order = paid_setup
    # The customer is waiting on the business.
    Message.objects.create(
        account=account, conversation=conversation, direction=Message.Direction.INBOUND,
        body="Have you got my payment?", timestamp=timezone.now(),
    )
    assert conversation.pk in [c.pk for c in needs_attention(account, timezone.now())]

    payment = request_payment(order, redirect_url="https://example.com/thanks")
    mark_paid(payment, transaction_id="tx_1")

    # A payment note is not a reply — the customer is still waiting for one.
    assert conversation.pk in [c.pk for c in needs_attention(account, timezone.now())]


@pytest.mark.django_db
def test_payment_without_a_conversation_is_not_an_error(db):
    account = Account.objects.create(company_name="No Chat", slug="nc")
    contact = Contact.objects.create(account=account, email="a@example.com")
    order = create_order(account, contact, [{"name": "Thing", "unit_price": "10", "quantity": 1}])
    payment = request_payment(order, redirect_url="https://example.com/thanks")

    mark_paid(payment, transaction_id="tx_1")
    order.refresh_from_db()
    assert order.status == order.Status.PAID


@pytest.mark.django_db
def test_replayed_payment_webhook_does_not_duplicate_the_line(paid_setup):
    account, contact, conversation, order = paid_setup
    payment = request_payment(order, redirect_url="https://example.com/thanks")
    mark_paid(payment, transaction_id="tx_1")
    mark_paid(payment, transaction_id="tx_1")

    assert conversation.messages.filter(direction=Message.Direction.SYSTEM).count() == 1

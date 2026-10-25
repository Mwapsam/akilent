"""A settled order (paid, refunded, cancelled) is never sent back to awaiting payment."""

from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.accounts.models import Account
from apps.commerce.models import Order, Payment
from apps.commerce.services import (
    PaymentNotRequestable,
    create_order,
    request_payment,
)
from apps.contacts.models import Contact
from apps.core.actions import ActionError, run_action

pytestmark = pytest.mark.django_db


@pytest.fixture
def order():
    account = Account.objects.create(company_name="Guard Shop")
    contact = Contact.objects.create(account=account, phone="+260971234567")
    return create_order(
        account,
        contact,
        items=[{"name": "Sandals", "unit_price": Decimal("45.00"), "quantity": 1}],
    )


@pytest.mark.parametrize(
    "status", [Order.Status.PAID, Order.Status.REFUNDED, Order.Status.CANCELLED]
)
def test_a_settled_order_refuses_a_new_payment_request(order, status):
    order.status = status
    order.save(update_fields=["status"])

    with (
        patch("apps.billing.flutterwave.get_fw_client") as fw,
        pytest.raises(PaymentNotRequestable),
    ):
        request_payment(order, redirect_url="https://example.com/")

    order.refresh_from_db()
    assert order.status == status
    assert not Payment.objects.filter(order=order).exists()
    fw.assert_not_called()


def test_the_action_reports_it_as_an_ordinary_error(order):
    order.status = Order.Status.PAID
    order.save(update_fields=["status"])

    with pytest.raises(ActionError, match="already paid"):
        run_action(
            "request_payment",
            {"account": order.account},
            order=order,
            redirect_url="https://example.com/",
        )


def test_an_open_order_still_gets_a_link(order):
    with patch("apps.billing.flutterwave.get_fw_client") as fw:
        fw.return_value.initialize_payment.return_value = "https://checkout.example/1"
        payment = request_payment(order, redirect_url="https://example.com/")

    order.refresh_from_db()
    assert order.status == Order.Status.AWAITING_PAYMENT
    assert payment.checkout_url == "https://checkout.example/1"


def test_a_stale_copy_cannot_reopen_an_order_paid_since_it_was_loaded(order):
    """The order view or a workflow loaded the order while pending; the payment webhook
    then marked it paid. The stale copy must not pass the guard."""
    stale = Order.objects.get(pk=order.pk)
    Order.objects.filter(pk=order.pk).update(
        status=Order.Status.PAID, paid_at=order.created_at
    )

    with (
        patch("apps.billing.flutterwave.get_fw_client") as fw,
        pytest.raises(PaymentNotRequestable, match="already paid"),
    ):
        request_payment(stale, redirect_url="https://example.com/")

    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert not Payment.objects.filter(order=order).exists()
    fw.assert_not_called()

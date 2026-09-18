from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.accounts.models import Account
from apps.automation.models import WorkflowRun
from apps.commerce.models import Order, Payment
from apps.commerce.services import create_order, mark_paid
from apps.contacts.models import Contact
from apps.crm.models import Lead
from apps.verticals.services import activate_vertical


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971234567")


@pytest.mark.django_db
def test_confirm_and_charge_workflow_requests_payment_on_order_created(account, contact):
    activate_vertical(account, "restaurant")

    with patch("apps.billing.flutterwave.get_fw_client") as fw:
        fw.return_value.initialize_payment.return_value = "https://pay.example/abc"
        order = create_order(account, contact, items=[
            {"name": "Chicken Burger", "unit_price": Decimal("75.00"), "quantity": 2},
        ])

    order.refresh_from_db()
    assert order.status == Order.Status.AWAITING_PAYMENT
    payment = Payment.objects.get(order=order)
    assert payment.checkout_url == "https://pay.example/abc"


@pytest.mark.django_db
def test_win_back_workflow_enrolls_and_waits_on_order_paid(account, contact):
    activate_vertical(account, "restaurant")

    with patch("apps.billing.flutterwave.get_fw_client") as fw:
        fw.return_value.initialize_payment.return_value = "https://pay.example/abc"
        order = create_order(account, contact, items=[
            {"name": "Coke", "unit_price": Decimal("15.00"), "quantity": 1},
        ])
    payment = Payment.objects.get(order=order)
    mark_paid(payment, transaction_id="tx-1")

    run = WorkflowRun.objects.get(
        workflow__slug="restaurant-win-back", contact=contact,
    )
    assert run.status == WorkflowRun.Status.WAITING
    assert Lead.objects.filter(account=account, contact=contact).count() == 0

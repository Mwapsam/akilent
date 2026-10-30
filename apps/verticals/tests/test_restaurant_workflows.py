from decimal import Decimal

import pytest

from apps.accounts.models import Account
from apps.automation.models import WorkflowRun
from apps.commerce.models import Order, Payment
from apps.commerce.services import create_order, mark_order_paid
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
def test_a_new_order_asks_nobody_for_money(account, contact):
    """The pack no longer requests payment: Akilent doesn't take payments for businesses."""
    activate_vertical(account, "restaurant")
    order = create_order(
        account,
        contact,
        items=[
            {"name": "Chicken Burger", "unit_price": Decimal("75.00"), "quantity": 2}
        ],
    )

    order.refresh_from_db()
    assert order.status == Order.Status.PENDING
    assert not Payment.objects.filter(order=order).exists()
    assert not WorkflowRun.objects.filter(
        workflow__slug="restaurant-confirm-and-charge"
    ).exists()


@pytest.mark.django_db
def test_win_back_workflow_enrolls_and_waits_on_order_paid(account, contact):
    activate_vertical(account, "restaurant")
    order = create_order(
        account,
        contact,
        items=[{"name": "Coke", "unit_price": Decimal("15.00"), "quantity": 1}],
    )
    mark_order_paid(order, method="cash")

    run = WorkflowRun.objects.get(
        workflow__slug="restaurant-win-back",
        contact=contact,
    )
    assert run.status == WorkflowRun.Status.WAITING
    assert Lead.objects.filter(account=account, contact=contact).count() == 0

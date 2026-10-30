from decimal import Decimal

import pytest

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun
from apps.commerce.models import Order, OrderItem, Payment
from apps.commerce.services import create_order, mark_order_paid, mark_order_unpaid
from apps.contacts.models import Contact
from apps.conversations.models import Event
from apps.core.actions import ActionError, run_action


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def contact(account):
    return Contact.objects.create(
        account=account, phone="+260971234567", email="j@x.com"
    )


@pytest.fixture
def order(account, contact):
    return create_order(
        account,
        contact,
        items=[
            {"name": "Chicken Burger", "unit_price": Decimal("75.00"), "quantity": 2},
            {"name": "Coke", "unit_price": Decimal("15.00"), "quantity": 2},
        ],
    )


@pytest.mark.django_db
def test_create_order_computes_total_and_emits_event(order):
    assert order.total == Decimal("180.00")
    assert OrderItem.objects.filter(order=order).count() == 2
    assert Event.objects.filter(
        type="order.created", subject_id=order.public_id
    ).exists()


@pytest.mark.django_db
def test_create_order_requires_items(account, contact):
    with pytest.raises(ValueError):
        create_order(account, contact, items=[])


@pytest.mark.django_db
def test_mark_order_paid_records_how_and_emits_once(order):
    assert mark_order_paid(order, method="mtn_momo", reference="TX 991") is True
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert order.paid_at is not None
    assert (order.paid_method, order.paid_reference) == ("mtn_momo", "TX 991")
    # Akilent records the payment; it never takes one.
    assert not Payment.objects.filter(order=order).exists()
    assert (
        Event.objects.filter(type="order.paid", subject_id=order.public_id).count() == 1
    )

    # A second click (or a teammate at the same moment) changes nothing.
    assert mark_order_paid(order, method="cash") is False
    order.refresh_from_db()
    assert order.paid_method == "mtn_momo"
    assert (
        Event.objects.filter(type="order.paid", subject_id=order.public_id).count() == 1
    )


@pytest.mark.django_db
def test_an_unknown_method_is_stored_as_blank(order):
    mark_order_paid(order, method="bitcoin")
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert order.paid_method == ""


@pytest.mark.django_db
@pytest.mark.parametrize("status", [Order.Status.REFUNDED, Order.Status.CANCELLED])
def test_a_closed_order_is_not_marked_paid(order, status):
    Order.objects.filter(pk=order.pk).update(status=status)
    assert mark_order_paid(order, method="cash") is False
    order.refresh_from_db()
    assert order.status == status
    assert not Event.objects.filter(type="order.paid").exists()


@pytest.mark.django_db
def test_mark_order_unpaid_reopens_a_mistake(order):
    mark_order_paid(order, method="cash", reference="r1")
    assert mark_order_unpaid(order) is True
    order.refresh_from_db()
    assert order.status == Order.Status.PENDING
    assert (order.paid_at, order.paid_method, order.paid_reference) == (None, "", "")
    assert Event.objects.filter(
        type="order.unpaid", subject_id=order.public_id
    ).exists()
    # Only a paid order can be reopened.
    assert mark_order_unpaid(order) is False


@pytest.mark.django_db
def test_order_paid_enrolls_published_workflow(account, contact, order):
    Workflow.objects.create(
        account=account,
        name="Notify kitchen",
        slug="notify-kitchen",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "order.paid"},
            "steps": [{"id": "stop", "type": "stop"}],
        },
    )
    mark_order_paid(order, method="cash")

    assert WorkflowRun.objects.filter(contact=contact).exists()


@pytest.mark.django_db
def test_action_registry_creates_orders_but_cannot_request_payment(account, contact):
    result = run_action(
        "create_order",
        {},
        account=account,
        contact=contact,
        items=[{"name": "Widget", "unit_price": Decimal("10.00"), "quantity": 1}],
    )
    order = Order.objects.get(public_id=result["order_id"])
    assert order.total == Decimal("10.00")

    # Payment links are retired: no automation can ask a customer for money.
    with pytest.raises(ActionError):
        run_action("request_payment", {}, order=order, redirect_url="https://x/")


@pytest.mark.django_db
def test_action_registry_create_order_rejects_empty_items(account, contact):
    with pytest.raises(ActionError):
        run_action("create_order", {}, account=account, contact=contact, items=[])


# Phase 0 - each order gets its own workflow run; a second order is not dropped.
def _order_workflow(account, trigger):
    return Workflow.objects.create(
        account=account,
        name="Order flow",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": trigger},
            "steps": [
                {"id": "w", "type": "wait", "seconds": 3600, "next": "done"},
                {"id": "done", "type": "stop"},
            ],
        },
    )


def _new_order(account, contact):
    return create_order(
        account,
        contact,
        items=[{"name": "Coke", "unit_price": Decimal("15.00"), "quantity": 1}],
    )


@pytest.mark.django_db
def test_each_order_created_gets_its_own_workflow_run(account, contact):
    wf = _order_workflow(account, "order.created")
    first, second = (
        _new_order(account, contact),
        _new_order(account, contact),
    )  # first still WAITING
    runs = WorkflowRun.objects.filter(workflow=wf, contact=contact)
    assert runs.count() == 2
    assert {r.context["order_id"] for r in runs} == {first.public_id, second.public_id}


@pytest.mark.django_db
def test_each_paid_order_gets_its_own_order_paid_run(account, contact):
    wf = _order_workflow(account, "order.paid")
    orders = [_new_order(account, contact), _new_order(account, contact)]
    for o in orders:
        mark_order_paid(o, method="cash")
    assert WorkflowRun.objects.filter(workflow=wf, contact=contact).count() == 2


@pytest.mark.django_db
def test_same_order_does_not_start_a_second_run(
    account, contact
):  # regression: still de-duplicated
    from apps.automation.workflow_engine import enroll_for_trigger

    wf = _order_workflow(account, "order.created")
    order = _new_order(account, contact)
    enroll_for_trigger(
        account.id,
        "order.created",
        contact,
        context={"order_id": order.public_id},
        subject_key=f"order:{order.public_id}",
    )
    assert WorkflowRun.objects.filter(workflow=wf, contact=contact).count() == 1


@pytest.mark.django_db
def test_triggers_without_a_subject_stay_one_active_run_per_contact(
    account, contact
):  # regression
    from apps.automation.workflow_engine import enroll_for_trigger

    wf = _order_workflow(account, "contact.updated")
    enroll_for_trigger(account.id, "contact.updated", contact)
    enroll_for_trigger(account.id, "contact.updated", contact)
    assert WorkflowRun.objects.filter(workflow=wf, contact=contact).count() == 1

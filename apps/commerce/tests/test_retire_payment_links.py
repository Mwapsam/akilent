"""The data migration that retired order payment links (0005_retire_payment_links)."""

import importlib
from decimal import Decimal

import pytest
from django.apps import apps as django_apps

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun, WorkflowVersion
from apps.commerce.models import Order, Payment
from apps.commerce.services import create_order
from apps.contacts.models import Contact

migration = importlib.import_module(
    "apps.commerce.migrations.0005_retire_payment_links"
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    return Account.objects.create(company_name="Retire Shop")


def _order(account, status):
    contact = Contact.objects.create(
        account=account, phone=f"+26097{Contact.objects.count():07d}"
    )
    order = create_order(
        account,
        contact,
        items=[{"name": "Dress", "unit_price": Decimal("45.00"), "quantity": 1}],
    )
    Order.objects.filter(pk=order.pk).update(status=status)
    order.refresh_from_db()
    return order


def _workflow(account, slug, steps, status=Workflow.Status.PUBLISHED):
    return Workflow.objects.create(
        account=account,
        name=slug,
        slug=slug,
        status=status,
        definition={"trigger": {"type": "order.created"}, "steps": steps},
    )


def test_waiting_orders_reopen_and_their_links_are_withdrawn(account):
    waiting = _order(account, Order.Status.AWAITING_PAYMENT)
    link = Payment.objects.create(
        account=account, order=waiting, amount=waiting.total, checkout_url="https://x/1"
    )
    paid = _order(account, Order.Status.PAID)
    taken = Payment.objects.create(
        account=account,
        order=paid,
        amount=paid.total,
        status=Payment.Status.SUCCEEDED,
        transaction_id="fw-9",
    )

    migration.retire(django_apps, None)

    waiting.refresh_from_db()
    link.refresh_from_db()
    paid.refresh_from_db()
    taken.refresh_from_db()
    assert waiting.status == Order.Status.PENDING
    assert link.status == Payment.Status.FAILED
    assert link.raw_payload["error"] == "withdrawn"
    # History is left as it is.
    assert paid.status == Order.Status.PAID
    assert taken.status == Payment.Status.SUCCEEDED


def test_automations_that_request_payment_are_archived(account):
    charging = _workflow(
        account,
        "restaurant-confirm-and-charge",
        [
            {"id": "c", "type": "action", "action": "request_payment", "next": "d"},
            {"id": "d", "type": "stop"},
        ],
    )
    other = _workflow(
        account,
        "notify",
        [{"id": "n", "type": "action", "action": "add_internal_note"}],
    )

    migration.retire(django_apps, None)

    charging.refresh_from_db()
    other.refresh_from_db()
    assert charging.status == Workflow.Status.ARCHIVED
    assert other.status == Workflow.Status.PUBLISHED


def test_a_nested_request_payment_step_is_found():
    assert migration._uses_request_payment(
        {"steps": [{"branches": [{"steps": [{"action": "request_payment"}]}]}]}
    )
    assert not migration._uses_request_payment({"steps": [{"action": "create_lead"}]})


def test_failed_orders_reopen_too(account):
    failed = _order(account, Order.Status.FAILED)
    migration.retire(django_apps, None)
    failed.refresh_from_db()
    assert failed.status == Order.Status.PENDING


def test_runs_in_progress_on_a_charging_version_are_cancelled(account):
    charging_steps = [
        {"id": "c", "type": "action", "action": "request_payment", "next": "d"},
        {"id": "d", "type": "stop"},
    ]
    # Edited since: the live definition no longer charges, but a waiting run is still
    # pinned to the version that does.
    workflow = _workflow(account, "edited", [{"id": "d", "type": "stop"}])
    version = WorkflowVersion.objects.create(
        workflow=workflow, number=99, digest="x", definition={"steps": charging_steps}
    )
    contact = Contact.objects.create(account=account, phone="+260979999999")
    pinned = WorkflowRun.objects.create(
        workflow=workflow,
        contact=contact,
        version=version,
        status=WorkflowRun.Status.WAITING,
        subject_key="order:1",
    )
    archived = _workflow(account, "charging", charging_steps)
    active = WorkflowRun.objects.create(
        workflow=archived, contact=contact, status=WorkflowRun.Status.ACTIVE
    )
    done = WorkflowRun.objects.create(
        workflow=archived,
        contact=contact,
        status=WorkflowRun.Status.COMPLETED,
        subject_key="order:2",
    )

    migration.retire(django_apps, None)

    for run in (pinned, active, done):
        run.refresh_from_db()
    assert pinned.status == WorkflowRun.Status.CANCELLED
    assert active.status == WorkflowRun.Status.CANCELLED
    assert done.status == WorkflowRun.Status.COMPLETED
    workflow.refresh_from_db()
    assert workflow.status == Workflow.Status.PUBLISHED

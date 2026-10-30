"""Regressions from the slice 2-0 review: re-marking paid, retracting a mistaken sale, the
paid-online guard, the business's own payment method, and late charges on retired links."""

import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.accounts import profile
from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun
from apps.commerce.models import Order, Payment
from apps.commerce.services import (
    PaidOnline,
    create_order,
    mark_order_paid,
    mark_order_unpaid,
    payment_method_choices,
    payment_method_label,
)
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Event, Message
from apps.insights.models import RecommendationLog

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    return Account.objects.create(company_name="Review Shop")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971234567")


@pytest.fixture
def conversation(account, contact):
    return Conversation.objects.create(
        account=account,
        contact=contact,
        channel=Conversation.Channel.WHATSAPP,
        last_message_at=timezone.now(),
    )


@pytest.fixture
def order(account, contact):
    return create_order(
        account,
        contact,
        items=[{"name": "Dress", "unit_price": Decimal("100.00"), "quantity": 1}],
        currency="ZMW",
    )


def _notes(conversation):
    return conversation.messages.filter(direction=Message.Direction.SYSTEM).count()


def test_marking_paid_again_after_a_mistake_does_not_rerun_workflows(
    account, contact, conversation, order
):
    wf = Workflow.objects.create(
        account=account,
        name="Thank you",
        slug="thanks",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "order.paid"},
            "steps": [{"id": "stop", "type": "stop"}],
        },
    )
    mark_order_paid(order, method="cash")
    assert WorkflowRun.objects.filter(workflow=wf).count() == 1  # run finished
    mark_order_unpaid(order)

    assert mark_order_paid(order, method="cash") is True
    order.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert WorkflowRun.objects.filter(workflow=wf).count() == 1
    assert (
        Event.objects.filter(type="order.paid", subject_id=order.public_id).count() == 1
    )
    assert _notes(conversation) == 1


def test_marking_unpaid_retracts_the_insight_revenue_and_paying_restores_it(order):
    rec = RecommendationLog.objects.create(account=order.account)
    with patch("apps.commerce.services._order_rec_log", return_value=rec):
        mark_order_paid(order, method="cash")
        rec.refresh_from_db()
        assert rec.outcome_summary["order_paid_at"]
        assert rec.outcome_summary["revenue"] == "100.00"

        mark_order_unpaid(order)
        rec.refresh_from_db()
        assert "order_paid_at" not in rec.outcome_summary
        assert "revenue" not in rec.outcome_summary

        mark_order_paid(order, method="cash")
        rec.refresh_from_db()
        assert rec.outcome_summary["revenue"] == "100.00"


def test_the_service_refuses_to_unpay_an_order_paid_online(order):
    Payment.objects.create(
        account=order.account,
        order=order,
        amount=order.total,
        status=Payment.Status.SUCCEEDED,
        transaction_id="fw-1",
    )
    Order.objects.filter(pk=order.pk).update(status=Order.Status.PAID)
    order.refresh_from_db()
    with pytest.raises(PaidOnline):
        mark_order_unpaid(order)
    order.refresh_from_db()
    assert order.status == Order.Status.PAID


def test_the_view_says_why_unpaid_was_refused(client, order):
    from django.contrib.auth.models import User

    from apps.accounts.models import Membership

    user = User.objects.create_user("o", "o@example.com", "pw")
    Membership.objects.create(
        user=user, account=order.account, role=Membership.Role.OWNER
    )
    client.force_login(user)
    Payment.objects.create(
        account=order.account,
        order=order,
        amount=order.total,
        status=Payment.Status.SUCCEEDED,
        transaction_id="fw-1",
    )
    Order.objects.filter(pk=order.pk).update(status=Order.Status.PAID)
    page = client.post(
        f"/orders/{order.public_id}/", {"action": "mark_unpaid"}, follow=True
    )
    assert b"paid online" in page.content


def test_the_business_own_method_is_offered_and_kept_as_typed(account, order):
    profile.save_profile(account, payment_other="Zanaco bank transfer")
    choices = payment_method_choices(account)
    assert ("Zanaco bank transfer", "Zanaco bank transfer") in choices
    assert ("mtn_momo", "MTN MoMo") not in choices  # it listed no built-in method
    assert choices[-1] == ("other", "Other")

    mark_order_paid(order, method="Zanaco bank transfer")
    order.refresh_from_db()
    assert order.paid_method == "Zanaco bank transfer"
    assert payment_method_label(order.paid_method) == "Zanaco bank transfer"


def test_with_no_methods_listed_every_built_in_one_is_offered(account):
    keys = [k for k, _ in payment_method_choices(account)]
    assert keys == [*profile.PAYMENT_METHODS, "other"]


# --- A customer pays an old link after links were retired ------------------------------


def _charge(client, order, payment, tx_id=555):
    payload = {
        "event": "charge.completed",
        "data": {
            "id": tx_id,
            "status": "successful",
            "amount": str(order.total),
            "meta": {"order_id": order.public_id, "payment_id": payment.public_id},
        },
    }
    return client.post(
        "/billing/webhook/",
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_VERIF_HASH="test-hash",
    )


def test_a_late_charge_on_a_retired_link_is_recorded_and_flagged_for_refund(
    client, conversation, order
):
    payment = Payment.objects.create(
        account=order.account, order=order, amount=order.total, currency="ZMW"
    )
    verified = {"status": "successful", "amount": "100.00", "currency": "ZMW"}
    with (
        patch("apps.billing.providers.flutterwave.get_fw_client") as fw,
        patch("apps.commerce.services.logger") as log,
    ):
        fw.return_value.verify_transaction.return_value = verified
        assert _charge(client, order, payment).status_code == 200
        assert _charge(client, order, payment).status_code == 200  # a retry

    payment.refresh_from_db()
    order.refresh_from_db()
    assert payment.status == Payment.Status.SUCCEEDED
    assert payment.raw_payload["refund_due"] is True
    # The business collects payment itself; the order isn't marked paid for it.
    assert order.status == Order.Status.PENDING
    note = conversation.messages.get(direction=Message.Direction.SYSTEM)
    assert "refunded" in note.body
    log.error.assert_called_once()
    assert "refund it to the customer" in log.error.call_args.args[0]

    # The business marks it paid once the customer pays them directly, and can undo that.
    page = client.get(f"/orders/{order.public_id}/")  # not logged in: just the service
    assert page.status_code in (302, 403)
    mark_order_paid(order, method="cash")
    assert mark_order_unpaid(order) is True


def test_a_rejected_charge_on_a_retired_link_records_nothing(client, order):
    payment = Payment.objects.create(
        account=order.account, order=order, amount=order.total, currency="ZMW"
    )
    with patch("apps.billing.providers.flutterwave.get_fw_client") as fw:
        fw.return_value.verify_transaction.return_value = {"status": "failed"}
        assert _charge(client, order, payment).status_code == 200
    payment.refresh_from_db()
    assert payment.status == Payment.Status.PENDING

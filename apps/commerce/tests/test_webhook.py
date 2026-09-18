"""The commerce charge-completed branch of the shared Flutterwave webhook
(apps/billing/views.py:webhook) — same endpoint, verif-hash check, and
ProcessedWebhookEvent idempotency ledger as subscription billing, routed by
``meta.order_id`` instead of ``meta.account_id``.
"""
import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.accounts.models import Account
from apps.billing.models import ProcessedWebhookEvent
from apps.commerce.models import Order, Payment
from apps.commerce.services import create_order
from apps.contacts.models import Contact

WEBHOOK_URL = "/billing/webhook/"


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971234567")


@pytest.fixture
def order(account, contact):
    return create_order(account, contact, items=[
        {"name": "Chicken Burger", "unit_price": Decimal("100.00"), "quantity": 1},
    ])


@pytest.fixture
def payment(account, order):
    return Payment.objects.create(account=account, order=order, amount=order.total)


def _charge_payload(payment, order, *, tx_id=777, amount="100.00"):
    return {
        "event": "charge.completed",
        "data": {
            "id": tx_id,
            "status": "successful",
            "amount": amount,
            "customer": {"email": "buyer@example.com"},
            "meta": {"order_id": order.public_id, "payment_id": payment.public_id},
        },
    }


def _post(client, payload):
    return client.post(
        WEBHOOK_URL, data=json.dumps(payload), content_type="application/json",
        HTTP_VERIF_HASH="test-hash",
    )


@pytest.mark.django_db
def test_commerce_charge_marks_order_paid(client, account, order, payment):
    verified = {"status": "successful", "amount": "100.00"}
    with patch("apps.billing.views.get_fw_client") as fw:
        fw.return_value.verify_transaction.return_value = verified
        resp = _post(client, _charge_payload(payment, order))

    assert resp.status_code == 200
    order.refresh_from_db()
    payment.refresh_from_db()
    assert order.status == Order.Status.PAID
    assert payment.status == Payment.Status.SUCCEEDED


@pytest.mark.django_db
def test_commerce_charge_is_idempotent_on_replay(client, account, order, payment):
    verified = {"status": "successful", "amount": "100.00"}
    with patch("apps.billing.views.get_fw_client") as fw:
        fw.return_value.verify_transaction.return_value = verified
        _post(client, _charge_payload(payment, order))
        resp = _post(client, _charge_payload(payment, order))

    assert resp.status_code == 200
    assert fw.return_value.verify_transaction.call_count == 1
    assert ProcessedWebhookEvent.objects.filter(event_key="charge.completed:777").count() == 1


@pytest.mark.django_db
def test_commerce_charge_rejected_when_amount_underpays(client, account, order, payment):
    verified = {"status": "successful", "amount": "5.00"}
    with patch("apps.billing.views.get_fw_client") as fw:
        fw.return_value.verify_transaction.return_value = verified
        resp = _post(client, _charge_payload(payment, order, amount="5.00"))

    assert resp.status_code == 200
    order.refresh_from_db()
    payment.refresh_from_db()
    assert order.status == Order.Status.FAILED
    assert payment.status == Payment.Status.FAILED


@pytest.mark.django_db
def test_commerce_charge_does_not_activate_subscription_path(client, account, order, payment):
    """A commerce charge must be routed away from the subscription handler
    entirely — regression guard for the meta.order_id branch in the shared
    webhook view."""
    verified = {"status": "successful", "amount": "100.00"}
    with patch("apps.billing.views.get_fw_client") as fw, \
         patch("apps.billing.views._handle_charge_completed") as legacy_handler:
        fw.return_value.verify_transaction.return_value = verified
        _post(client, _charge_payload(payment, order))

    legacy_handler.assert_not_called()

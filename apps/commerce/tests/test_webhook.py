"""A late charge on a retired order payment link (``meta.order_id``) reaching the shared
Flutterwave webhook. Akilent no longer takes payments for businesses, so the charge is
verified, recorded for a refund, and never marks the order paid or touches subscriptions.
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
    return create_order(
        account,
        contact,
        items=[
            {"name": "Chicken Burger", "unit_price": Decimal("100.00"), "quantity": 1},
        ],
    )


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
        WEBHOOK_URL,
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_VERIF_HASH="test-hash",
    )


@pytest.mark.django_db
def test_a_charge_on_a_retired_link_is_verified_once_and_kept_for_refund(
    client, account, order, payment
):
    verified = {"status": "successful", "amount": "100.00", "currency": order.currency}
    with (
        patch("apps.billing.providers.flutterwave.get_fw_client") as fw,
        patch("apps.billing.providers.flutterwave._legacy_charge") as legacy_handler,
        patch("apps.billing.payments.record_result") as record_result,
    ):
        fw.return_value.verify_transaction.return_value = verified
        resp = _post(client, _charge_payload(payment, order))
        replay = _post(client, _charge_payload(payment, order))

    assert resp.status_code == 200
    assert replay.status_code == 200
    order.refresh_from_db()
    payment.refresh_from_db()
    assert order.status == Order.Status.PENDING
    assert payment.status == Payment.Status.SUCCEEDED
    assert payment.raw_payload["refund_due"] is True
    assert fw.return_value.verify_transaction.call_count == 1
    # Never routed to subscription billing.
    legacy_handler.assert_not_called()
    record_result.assert_not_called()
    assert (
        ProcessedWebhookEvent.objects.filter(
            event_key="flutterwave:charge.completed:777"
        ).count()
        == 1
    )

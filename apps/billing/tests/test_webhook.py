"""Flutterwave webhooks (old /billing/webhook/ URL and /billing/webhooks/flutterwave/): signature,
verify-before-trust, replay idempotency, and the amount/currency check, all against our own
BillingPayment reference rather than metadata in the body."""

import json
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.billing import payments
from apps.billing.models import BillingPayment, ProcessedWebhookEvent, Subscription

WEBHOOK_URL = "/billing/webhook/"
FW = "apps.billing.providers.flutterwave.get_fw_client"


@pytest.fixture
def pending(account, plan, subscription):
    return payments.create_payment(
        account, plan, Subscription.MONTHLY, provider="flutterwave"
    )


def _payload(tx_ref, *, tx_id=555):
    return {"event": "charge.completed", "data": {"id": tx_id, "tx_ref": tx_ref}}


def _verified(
    tx_ref, *, amount="19.00", currency="USD", status="successful", card=None
):
    data = {
        "id": 555,
        "tx_ref": tx_ref,
        "status": status,
        "amount": amount,
        "currency": currency,
        "customer": {"email": "buyer@example.com"},
    }
    if card:
        data["card"] = card
    return data


def _post(client, payload, url=WEBHOOK_URL, verif_hash="test-hash"):
    return client.post(
        url,
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_VERIF_HASH=verif_hash,
    )


@pytest.mark.django_db
def test_webhook_rejects_bad_signature(client):
    assert _post(client, {}, verif_hash="wrong").status_code == 401
    assert (
        _post(
            client, {}, url="/billing/webhooks/flutterwave/", verif_hash=""
        ).status_code
        == 401
    )


@pytest.mark.django_db
def test_charge_activates_subscription_and_saves_card(
    client, account, pending, subscription
):
    card = {
        "token": "flw-t1-abc",
        "type": "VISA",
        "last_4digits": "4242",
        "expiry": "09/30",
    }
    with patch(FW) as fw:
        fw.return_value.verify_transaction.return_value = _verified(
            pending.reference, card=card
        )
        resp = _post(client, _payload(pending.reference))

    assert resp.status_code == 200
    subscription.refresh_from_db()
    pending.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE
    assert subscription.provider == "flutterwave"
    assert pending.status == BillingPayment.SUCCEEDED
    method = account.saved_payment_methods.get()
    assert method.token == "flw-t1-abc" and method.last4 == "4242"


@pytest.mark.django_db
def test_charge_is_idempotent_on_replay(client, pending, subscription):
    with patch(FW) as fw:
        fw.return_value.verify_transaction.return_value = _verified(pending.reference)
        _post(client, _payload(pending.reference))
        subscription.refresh_from_db()
        first_end = subscription.current_period_end
        resp = _post(client, _payload(pending.reference))
        subscription.refresh_from_db()

    assert resp.status_code == 200
    assert subscription.current_period_end == first_end
    assert (
        ProcessedWebhookEvent.objects.filter(
            event_key="flutterwave:charge.completed:555"
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_charge_rejected_when_amount_underpays(client, pending, subscription):
    with patch(FW) as fw:
        fw.return_value.verify_transaction.return_value = _verified(
            pending.reference, amount="5.00"
        )
        resp = _post(client, _payload(pending.reference))

    assert resp.status_code == 200  # acknowledged, but not honoured
    subscription.refresh_from_db()
    pending.refresh_from_db()
    assert subscription.status == Subscription.PAST_DUE
    assert pending.status == BillingPayment.FAILED


@pytest.mark.django_db
def test_charge_rejected_in_another_currency(client, pending, subscription):
    # 19 NGN is not $19.
    with patch(FW) as fw:
        fw.return_value.verify_transaction.return_value = _verified(
            pending.reference, currency="NGN"
        )
        _post(client, _payload(pending.reference))

    subscription.refresh_from_db()
    assert subscription.status == Subscription.PAST_DUE


@pytest.mark.django_db
def test_unknown_reference_grants_nothing(client, pending, subscription):
    with patch(FW) as fw:
        fw.return_value.verify_transaction.return_value = _verified("pay_someone_else")
        resp = _post(client, _payload("pay_someone_else"))

    assert resp.status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == Subscription.PAST_DUE


@pytest.mark.django_db
def test_verify_failure_is_retried_not_swallowed(client, pending, subscription):
    from apps.billing.flutterwave import FlutterwaveError

    with patch(FW) as fw:
        fw.return_value.verify_transaction.side_effect = FlutterwaveError("timeout")
        resp = _post(client, _payload(pending.reference))
    assert resp.status_code == 500  # Flutterwave retries
    assert not ProcessedWebhookEvent.objects.exists()

    with patch(FW) as fw:
        fw.return_value.verify_transaction.return_value = _verified(pending.reference)
        _post(client, _payload(pending.reference))
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE


@pytest.mark.django_db
def test_legacy_payment_plan_renewal_still_renews(client, account, plan, subscription):
    subscription.status = Subscription.ACTIVE
    subscription.fw_subscription_id = "9001"
    subscription.fw_customer_email = "buyer@example.com"
    subscription.save()
    with patch(FW) as fw:
        fw.return_value.verify_transaction.return_value = _verified(
            "flw-generated-ref", amount=str(Decimal("19.00"))
        )
        _post(client, _payload("flw-generated-ref", tx_id=556))

    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE
    assert subscription.current_period_end is not None

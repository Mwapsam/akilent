import json
from unittest.mock import patch

import pytest

from apps.billing.models import ProcessedWebhookEvent, Subscription

WEBHOOK_URL = "/billing/webhook/"


def _charge_payload(account, *, tx_id=555, amount="19.00", plan_slug="starter"):
    return {
        "event": "charge.completed",
        "data": {
            "id": tx_id,
            "status": "successful",
            "amount": amount,
            "customer": {"email": "buyer@example.com"},
            "meta": {"account_id": account.pk, "plan_slug": plan_slug},
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
def test_webhook_rejects_bad_signature(client):
    resp = client.post(
        WEBHOOK_URL, data="{}", content_type="application/json", HTTP_VERIF_HASH="wrong"
    )
    assert resp.status_code == 401


@pytest.mark.django_db
def test_charge_activates_subscription(client, account, plan, subscription):
    verified = {
        "status": "successful",
        "amount": "19.00",
        "customer": {"email": "buyer@example.com"},
    }
    with patch("apps.billing.views.get_fw_client") as fw:
        fw.return_value.verify_transaction.return_value = verified
        resp = _post(client, _charge_payload(account))

    assert resp.status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE


@pytest.mark.django_db
def test_charge_is_idempotent_on_replay(client, account, plan, subscription):
    verified = {
        "status": "successful",
        "amount": "19.00",
        "customer": {"email": "buyer@example.com"},
    }
    with patch("apps.billing.views.get_fw_client") as fw:
        fw.return_value.verify_transaction.return_value = verified
        _post(client, _charge_payload(account))
        subscription.refresh_from_db()
        first_end = subscription.current_period_end

        # Replay the identical event.
        resp = _post(client, _charge_payload(account))
        subscription.refresh_from_db()

    assert resp.status_code == 200
    # The period was not extended a second time, and we only verified once.
    assert subscription.current_period_end == first_end
    assert fw.return_value.verify_transaction.call_count == 1
    assert (
        ProcessedWebhookEvent.objects.filter(event_key="charge.completed:555").count()
        == 1
    )


@pytest.mark.django_db
def test_charge_rejected_when_amount_underpays(client, account, plan, subscription):
    # Flutterwave confirms only $5 was charged for a $19 plan — must not activate.
    verified = {
        "status": "successful",
        "amount": "5.00",
        "customer": {"email": "buyer@example.com"},
    }
    with patch("apps.billing.views.get_fw_client") as fw:
        fw.return_value.verify_transaction.return_value = verified
        resp = _post(client, _charge_payload(account, amount="5.00"))

    assert resp.status_code == 200  # acknowledged, but not honored
    subscription.refresh_from_db()
    assert subscription.status == Subscription.PAST_DUE

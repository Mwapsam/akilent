"""Stripe webhooks: checkout sessions (mode=payment, keyed by our reference), off-session
PaymentIntents, legacy subscription invoices (dahlia payload shape), and de-dupe behaviour."""

from unittest.mock import patch

import pytest

from apps.billing import payments
from apps.billing.models import BillingPayment, ProcessedWebhookEvent, Subscription

WEBHOOK_URL = "/billing/stripe/webhook/"
CONSTRUCT = "apps.billing.providers.stripe.stripe_sdk.Webhook.construct_event"
RETRIEVE_PI = "apps.billing.providers.stripe.stripe_sdk.PaymentIntent.retrieve"


@pytest.fixture(autouse=True)
def stripe_keys(settings):
    settings.STRIPE_SECRET_KEY = "sk_test"
    settings.STRIPE_WEBHOOK_SECRET = "whsec_test"


@pytest.fixture
def pending(account, plan, subscription):
    return payments.create_payment(
        account, plan, Subscription.QUARTERLY, provider="stripe"
    )


def _session_event(payment, *, event_id="evt_1", amount_total=None, status="paid"):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_1",
                "mode": "payment",
                "payment_status": status,
                "customer": "cus_123",
                "client_reference_id": payment.reference,
                "metadata": {"reference": payment.reference},
                "currency": "usd",
                "amount_total": int(payment.amount_due * 100)
                if amount_total is None
                else amount_total,
                "payment_intent": "pi_1",
            }
        },
    }


PI = {
    "id": "pi_1",
    "payment_method": {
        "id": "pm_1",
        "card": {"brand": "visa", "last4": "4242", "exp_month": 9, "exp_year": 2030},
    },
}


def _post(client, event):
    with patch(CONSTRUCT, return_value=event), patch(RETRIEVE_PI, return_value=PI):
        return client.post(
            WEBHOOK_URL,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )


@pytest.mark.django_db
def test_invalid_signature_is_rejected(client):
    import stripe

    with patch(
        CONSTRUCT, side_effect=stripe.error.SignatureVerificationError("bad", "sig")
    ):
        resp = client.post(WEBHOOK_URL, data=b"{}", content_type="application/json")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_webhook_without_secret_is_rejected(client, settings):
    settings.STRIPE_WEBHOOK_SECRET = ""
    resp = client.post(WEBHOOK_URL, data=b"{}", content_type="application/json")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_checkout_completed_activates_subscription(
    client, account, pending, subscription
):
    resp = _post(client, _session_event(pending))

    assert resp.status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE
    assert subscription.provider == "stripe"
    assert subscription.billing_period == "quarterly"
    method = account.saved_payment_methods.get()
    assert method.token == "pm_1" and method.customer_ref == "cus_123"


@pytest.mark.django_db
def test_checkout_completed_is_idempotent_on_replay(client, pending, subscription):
    event = _session_event(pending)
    _post(client, event)
    subscription.refresh_from_db()
    first_end = subscription.current_period_end

    resp = _post(client, event)
    subscription.refresh_from_db()

    assert resp.status_code == 200
    assert subscription.current_period_end == first_end
    assert (
        ProcessedWebhookEvent.objects.filter(
            event_key="stripe:checkout.session.completed:evt_1"
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_underpaid_session_does_not_activate(client, pending, subscription):
    _post(client, _session_event(pending, amount_total=100))
    subscription.refresh_from_db()
    pending.refresh_from_db()
    assert subscription.status == Subscription.PAST_DUE
    assert pending.status == BillingPayment.FAILED


@pytest.mark.django_db
def test_legacy_invoice_payment_failed_marks_past_due(client, subscription):
    """API dahlia: the subscription id is under parent.subscription_details."""
    subscription.status = Subscription.ACTIVE
    subscription.stripe_subscription_id = "sub_123"
    subscription.save()
    event = {
        "id": "evt_2",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "parent": {"subscription_details": {"subscription": "sub_123"}},
                "billing_reason": "subscription_cycle",
            }
        },
    }
    assert _post(client, event).status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == Subscription.PAST_DUE
    assert subscription.past_due_since is not None


@pytest.mark.django_db
def test_legacy_invoice_paid_renews(client, subscription):
    subscription.status = Subscription.ACTIVE
    subscription.stripe_subscription_id = "sub_123"
    subscription.save()
    event = {
        "id": "evt_3",
        "type": "invoice.payment_succeeded",
        "data": {
            "object": {
                "parent": {"subscription_details": {"subscription": "sub_123"}},
                "billing_reason": "subscription_cycle",
            }
        },
    }
    _post(client, event)
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE
    assert subscription.current_period_end is not None


@pytest.mark.django_db
def test_legacy_subscription_deleted_keeps_paid_access(client, subscription):
    """Akilent's renewal engine takes over; the paid period isn't cut short."""
    subscription.status = Subscription.ACTIVE
    subscription.stripe_subscription_id = "sub_123"
    subscription.save()
    event = {
        "id": "evt_4",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_123"}},
    }
    assert _post(client, event).status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE
    assert not subscription.stripe_subscription_id


@pytest.mark.django_db
def test_webhook_handles_non_dict_stripe_objects(client, pending, subscription):
    import stripe

    event = _session_event(pending)
    event["data"]["object"] = stripe.StripeObject.construct_from(
        event["data"]["object"], "sk_test"
    )
    assert _post(client, event).status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE


@pytest.mark.django_db
def test_failed_handler_does_not_burn_the_dedupe_key(client, pending, subscription):
    event = _session_event(pending)
    with patch(
        "apps.billing.payments.activate_subscription", side_effect=RuntimeError("db")
    ):
        with pytest.raises(RuntimeError):
            _post(client, event)
    assert not ProcessedWebhookEvent.objects.exists()

    _post(client, event)  # Stripe's retry is processed
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE

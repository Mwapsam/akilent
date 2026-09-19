from unittest.mock import patch

import pytest

from apps.billing.models import ProcessedWebhookEvent, Subscription

WEBHOOK_URL = "/billing/stripe/webhook/"


def _checkout_completed_event(account, *, plan_slug="starter", billing_period="quarterly", event_id="evt_1"):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "subscription",
                "payment_status": "paid",
                "customer": "cus_123",
                "subscription": "sub_123",
                "metadata": {
                    "account_id": str(account.pk),
                    "plan_slug": plan_slug,
                    "billing_period": billing_period,
                },
            }
        },
    }


def _post(client, event):
    with patch("apps.billing.views.stripe.Webhook.construct_event", return_value=event):
        return client.post(
            WEBHOOK_URL, data=b"{}", content_type="application/json", HTTP_STRIPE_SIGNATURE="sig"
        )


@pytest.mark.django_db
def test_checkout_completed_activates_subscription(client, account, plan, subscription, settings):
    settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
    resp = _post(client, _checkout_completed_event(account))

    assert resp.status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE
    assert subscription.payment_method == "stripe"
    assert subscription.billing_period == "quarterly"
    assert subscription.stripe_subscription_id == "sub_123"


@pytest.mark.django_db
def test_checkout_completed_is_idempotent_on_replay(client, account, plan, subscription, settings):
    settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
    event = _checkout_completed_event(account)

    _post(client, event)
    subscription.refresh_from_db()
    first_end = subscription.current_period_end

    resp = _post(client, event)
    subscription.refresh_from_db()

    assert resp.status_code == 200
    assert subscription.current_period_end == first_end
    assert ProcessedWebhookEvent.objects.filter(event_key="checkout.session.completed:evt_1").count() == 1


@pytest.mark.django_db
def test_invoice_payment_failed_marks_past_due(client, account, plan, subscription, settings):
    settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
    subscription.status = Subscription.ACTIVE
    subscription.stripe_subscription_id = "sub_123"
    subscription.save(update_fields=["status", "stripe_subscription_id"])

    event = {
        "id": "evt_2",
        "type": "invoice.payment_failed",
        "data": {"object": {"subscription": "sub_123"}},
    }
    resp = _post(client, event)

    assert resp.status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == Subscription.PAST_DUE


@pytest.mark.django_db
def test_subscription_deleted_cancels_locally(client, account, plan, subscription, settings):
    settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
    subscription.status = Subscription.ACTIVE
    subscription.stripe_subscription_id = "sub_123"
    subscription.save(update_fields=["status", "stripe_subscription_id"])

    event = {
        "id": "evt_3",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_123"}},
    }
    resp = _post(client, event)

    assert resp.status_code == 200
    subscription.refresh_from_db()
    assert subscription.status == Subscription.CANCELLED
    assert subscription.cancelled_at is not None

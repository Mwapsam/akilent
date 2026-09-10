import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, WebhookDelivery, WebhookEndpoint


@pytest.fixture
def setup(db):
    user = User.objects.create_user("o", "o@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"),
                               max_emails_per_month=100, email_apis=True, api_rate_per_min=0,
                               bulk_email=True, outbound_webhooks=True)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    _, raw = EmailApiKey.create_for_account(acc, name="k")
    return raw, acc


def _endpoint(account, events):
    return WebhookEndpoint.objects.create(
        account=account, url="https://hook.example/x", event_types=events
    )


def _post(client, key, path, body):
    return client.post(f"/api/v1{path}", data=json.dumps(body),
                       content_type="application/json", HTTP_X_API_KEY=key)


@pytest.mark.django_db
def test_contact_created_and_updated_fire_webhooks(client, setup):
    key, acc = setup
    _endpoint(acc, ["contact.created", "contact.updated"])

    _post(client, key, "/contacts", {"email": "a@x.com"})
    _post(client, key, "/contacts", {"email": "a@x.com", "first_name": "Ada"})

    types = list(
        WebhookDelivery.objects.filter(endpoint__account=acc).values_list("event_type", flat=True)
    )
    assert types.count("contact.created") == 1
    assert types.count("contact.updated") == 1


@pytest.mark.django_db
def test_unsubscribed_contact_fires_webhook(client, setup):
    key, acc = setup
    _endpoint(acc, ["contact.unsubscribed"])
    r = _post(client, key, "/contacts", {"email": "u@x.com"})
    cid = r.json()["id"]
    _post(client, key, f"/contacts/{cid}/events", {"type": "email.unsubscribed"})
    assert WebhookDelivery.objects.filter(
        endpoint__account=acc, event_type="contact.unsubscribed"
    ).count() == 1


@pytest.mark.django_db
def test_business_event_fires_event_received(client, setup):
    key, acc = setup
    _endpoint(acc, ["event.received"])
    _post(client, key, "/events", {"event": "invoice.paid", "customer": "b@x.com"})
    assert WebhookDelivery.objects.filter(
        endpoint__account=acc, event_type="event.received"
    ).count() == 1


@pytest.mark.django_db
def test_endpoint_only_receives_subscribed_events(client, setup):
    key, acc = setup
    _endpoint(acc, ["message.sent"])  # not subscribed to contact.created
    _post(client, key, "/contacts", {"email": "a@x.com"})
    assert WebhookDelivery.objects.filter(endpoint__account=acc).count() == 0


@pytest.mark.django_db
def test_webhook_test_endpoint(client, setup):
    key, acc = setup
    _endpoint(acc, ["contact.created"])
    r = _post(client, key, "/webhooks/test", {"event": "contact.created", "data": {"x": 1}})
    assert r.status_code == 202
    assert r.json()["deliveries"] == 1

    bad = _post(client, key, "/webhooks/test", {"event": "not.a.thing"})
    assert bad.status_code == 400

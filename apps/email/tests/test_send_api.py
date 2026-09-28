import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailDomain, EmailMessage

SEND_URL = "/email/send/"


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


def _subscribe(account, **plan_kwargs):
    defaults = dict(
        slug="p",
        name="P",
        price_monthly=Decimal("10"),
        max_emails_per_month=2,
        email_apis=True,
    )
    defaults.update(plan_kwargs)
    plan = Plan.objects.create(**defaults)
    return Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )


@pytest.fixture
def verified_domain(account):
    return EmailDomain.objects.create(
        account=account,
        domain="mail.acme.com",
        status=EmailDomain.Status.VERIFIED,
    )


@pytest.fixture
def api_key(account):
    obj, raw_key = EmailApiKey.create_for_account(account, name="default")
    obj.raw_key = raw_key  # test-only convenience attribute, not persisted
    return obj


def _payload(**overrides):
    body = {
        "from": "hello@mail.acme.com",
        "to": "recipient@example.com",
        "subject": "Hi",
        "text": "Hello there",
    }
    body.update(overrides)
    return body


@pytest.mark.django_db
def test_send_requires_api_key(client):
    resp = client.post(
        SEND_URL, data=json.dumps(_payload()), content_type="application/json"
    )
    assert resp.status_code == 401


@pytest.mark.django_db
def test_send_rejects_invalid_key(client, account, api_key):
    resp = client.post(
        SEND_URL,
        data=json.dumps(_payload()),
        content_type="application/json",
        HTTP_X_API_KEY="not-the-real-key",
    )
    assert resp.status_code == 401


@pytest.mark.django_db
def test_send_rejects_inactive_key(client, account, verified_domain, api_key):
    _subscribe(account)
    api_key.is_active = False
    api_key.save(update_fields=["is_active"])
    resp = client.post(
        SEND_URL,
        data=json.dumps(_payload()),
        content_type="application/json",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert resp.status_code == 401


@pytest.mark.django_db
def test_send_rejects_unverified_domain(client, account, api_key):
    _subscribe(account)
    resp = client.post(
        SEND_URL,
        data=json.dumps(_payload()),
        content_type="application/json",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert resp.status_code == 403
    assert "not a verified" in resp.json()["error"]


@pytest.mark.django_db
def test_send_rejects_when_plan_lacks_email_apis(
    client, account, verified_domain, api_key
):
    _subscribe(account, email_apis=False)
    resp = client.post(
        SEND_URL,
        data=json.dumps(_payload()),
        content_type="application/json",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert resp.status_code == 403
    assert "SMTP relay" in resp.json()["error"]


@pytest.mark.django_db
def test_send_rejects_when_quota_exhausted(client, account, verified_domain, api_key):
    _subscribe(account, max_emails_per_month=1)
    ok = client.post(
        SEND_URL,
        data=json.dumps(_payload()),
        content_type="application/json",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert ok.status_code == 202
    blocked = client.post(
        SEND_URL,
        data=json.dumps(_payload()),
        content_type="application/json",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert blocked.status_code == 403
    assert "limit" in blocked.json()["error"]


@pytest.mark.django_db
def test_send_accepts_valid_request_and_queues_message(
    client, account, verified_domain, api_key
):
    _subscribe(account)
    resp = client.post(
        SEND_URL,
        data=json.dumps(_payload()),
        content_type="application/json",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert resp.status_code == 202
    body = resp.json()
    msg = EmailMessage.objects.get(pk=body["id"])
    assert msg.status == EmailMessage.Status.QUEUED
    assert msg.from_email == "hello@mail.acme.com"

    api_key.refresh_from_db()
    assert api_key.last_used_at is not None


@pytest.mark.django_db
def test_send_accepts_bearer_auth_header(client, account, verified_domain, api_key):
    _subscribe(account)
    resp = client.post(
        SEND_URL,
        data=json.dumps(_payload()),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {api_key.raw_key}",
    )
    assert resp.status_code == 202


@pytest.mark.django_db
def test_send_requires_from_and_to(client, account, verified_domain, api_key):
    _subscribe(account)
    resp = client.post(
        SEND_URL,
        data=json.dumps(_payload(to="")),
        content_type="application/json",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert resp.status_code == 400

import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailDomain, EmailMessage

MESSAGES_URL = "/api/v1/messages"


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
        api_rate_per_min=0,
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
    obj.raw_key = raw_key
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


def _post(client, api_key, **payload_overrides):
    return client.post(
        MESSAGES_URL,
        data=json.dumps(_payload(**payload_overrides)),
        content_type="application/json",
        HTTP_X_API_KEY=api_key,
    )


@pytest.mark.django_db
def test_missing_key_returns_401_with_error_envelope(client):
    resp = client.post(
        MESSAGES_URL, data=json.dumps(_payload()), content_type="application/json"
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "authentication_failed"


@pytest.mark.django_db
def test_invalid_key_returns_401(client, account, api_key):
    resp = _post(client, "not-the-real-key")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_legacy_key_format_authenticates(client, account, verified_domain):
    _subscribe(account)
    legacy = EmailApiKey.objects.create(
        account=account, name="legacy", key="ek_legacyplaintextkey"
    )
    resp = _post(client, legacy.key)
    assert resp.status_code == 202


@pytest.mark.django_db
def test_unverified_domain_returns_403(client, account, api_key):
    _subscribe(account)
    resp = _post(client, api_key.raw_key)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "unverified_domain"


@pytest.mark.django_db
def test_plan_without_email_apis_returns_403(client, account, verified_domain, api_key):
    _subscribe(account, email_apis=False)
    resp = _post(client, api_key.raw_key)
    assert resp.status_code == 403


@pytest.mark.django_db
def test_quota_exhausted_returns_403(client, account, verified_domain, api_key):
    _subscribe(account, max_emails_per_month=1)
    ok = _post(client, api_key.raw_key)
    assert ok.status_code == 202
    blocked = _post(client, api_key.raw_key)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "emails"


@pytest.mark.django_db
def test_valid_request_queues_message(client, account, verified_domain, api_key):
    _subscribe(account)
    resp = _post(client, api_key.raw_key)
    assert resp.status_code == 202
    body = resp.json()
    msg = EmailMessage.objects.get(pk=body["id"])
    assert msg.status == EmailMessage.Status.QUEUED

    api_key.refresh_from_db()
    assert api_key.last_used_at is not None


@pytest.mark.django_db
def test_missing_from_field_returns_400(client, account, verified_domain, api_key):
    _subscribe(account)
    resp = _post(client, api_key.raw_key, **{"from": ""})
    assert resp.status_code == 400
    assert "error" in resp.json()


@pytest.mark.django_db
def test_unsupported_version_returns_404(client, account, verified_domain, api_key):
    _subscribe(account)
    resp = client.post(
        "/api/v2/messages",
        data=json.dumps(_payload()),
        content_type="application/json",
        HTTP_X_API_KEY=api_key.raw_key,
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_rate_throttle_blocks_after_limit(client, account, verified_domain, api_key):
    _subscribe(account, api_rate_per_min=1, max_emails_per_month=-1)
    first = _post(client, api_key.raw_key)
    assert first.status_code == 202
    second = _post(client, api_key.raw_key)
    assert second.status_code == 429
    assert "Retry-After" in second

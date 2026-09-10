import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailDomain, EmailMessage
from apps.logs.models import ApiRequest, IdempotencyRecord, MessageStatsDaily

MESSAGES_URL = "/api/v1/messages"


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(
        slug="p", name="P", price_monthly=Decimal("10"),
        max_emails_per_month=100, email_apis=True, api_rate_per_min=0,
    )
    Subscription.objects.create(
        account=acc, plan=plan, status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )
    EmailDomain.objects.create(
        account=acc, domain="mail.acme.com", status=EmailDomain.Status.VERIFIED,
    )
    return acc


@pytest.fixture
def api_key(account):
    obj, raw_key = EmailApiKey.create_for_account(account, name="default")
    return raw_key


def _payload(**overrides):
    body = {"from": "hello@mail.acme.com", "to": "r@example.com", "subject": "Hi", "text": "yo"}
    body.update(overrides)
    return body


def _post(client, key, headers=None, **overrides):
    return client.post(
        MESSAGES_URL,
        data=json.dumps(_payload(**overrides)),
        content_type="application/json",
        HTTP_X_API_KEY=key,
        **(headers or {}),
    )


@pytest.mark.django_db
def test_api_request_is_logged(client, account, api_key):
    resp = _post(client, api_key)
    assert resp.status_code == 202
    row = ApiRequest.objects.get()
    assert row.account_id == account.id
    assert row.method == "POST"
    assert row.status_code == 202
    assert row.path == MESSAGES_URL
    # API key must not be persisted in the header snapshot.
    assert "x-api-key" not in {k.lower() for k in row.request_headers}


@pytest.mark.django_db
def test_request_id_is_echoed(client, account, api_key):
    resp = _post(client, api_key, headers={"HTTP_X_REQUEST_ID": "trace-123"})
    assert resp["X-Request-Id"] == "trace-123"
    assert ApiRequest.objects.get().request_id == "trace-123"


@pytest.mark.django_db
def test_idempotency_replays_same_response(client, account, api_key):
    r1 = _post(client, api_key, headers={"HTTP_IDEMPOTENCY_KEY": "abc"})
    r2 = _post(client, api_key, headers={"HTTP_IDEMPOTENCY_KEY": "abc"})
    assert r1.status_code == r2.status_code == 202
    assert r1.json() == r2.json()
    assert EmailMessage.objects.count() == 1
    assert IdempotencyRecord.objects.filter(key="abc").count() == 1


@pytest.mark.django_db
def test_idempotency_key_reuse_with_different_body_conflicts(client, account, api_key):
    _post(client, api_key, headers={"HTTP_IDEMPOTENCY_KEY": "k"}, subject="one")
    resp = _post(client, api_key, headers={"HTTP_IDEMPOTENCY_KEY": "k"}, subject="two")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_key_reuse"
    assert EmailMessage.objects.count() == 1


@pytest.mark.django_db
def test_no_idempotency_key_allows_duplicates(client, account, api_key):
    _post(client, api_key)
    _post(client, api_key)
    assert EmailMessage.objects.count() == 2


@pytest.mark.django_db
def test_send_increments_daily_stats(client, account, api_key):
    from apps.logs.services import record_message_event

    _post(client, api_key)
    msg = EmailMessage.objects.get()
    record_message_event(msg, "delivered", source="ses_sns")
    record_message_event(msg, "opened", source="tracking_pixel")
    record_message_event(msg, "opened", source="tracking_pixel")

    row = MessageStatsDaily.objects.get(account=account, campaign__isnull=True)
    assert row.delivered == 1
    assert row.opened == 2
    assert row.unique_opens == 1


@pytest.mark.django_db
def test_message_list_and_detail_and_events(client, account, api_key):
    _post(client, api_key)
    _post(client, api_key, to="second@example.com")

    lst = client.get("/api/v1/messages", HTTP_X_API_KEY=api_key)
    assert lst.status_code == 200
    body = lst.json()
    assert body["total"] == 2
    pid = body["data"][0]["id"]
    assert pid.startswith("msg_")

    detail = client.get(f"/api/v1/messages/{pid}", HTTP_X_API_KEY=api_key)
    assert detail.status_code == 200
    assert detail.json()["id"] == pid
    assert "events" in detail.json()

    events = client.get(f"/api/v1/messages/{pid}/events", HTTP_X_API_KEY=api_key)
    assert events.status_code == 200
    types = [e["type"] for e in events.json()["data"]]
    assert "queued" in types


@pytest.mark.django_db
def test_message_list_filter_by_recipient(client, account, api_key):
    _post(client, api_key, to="alice@example.com")
    _post(client, api_key, to="bob@example.com")
    resp = client.get("/api/v1/messages?to=alice@example.com", HTTP_X_API_KEY=api_key)
    assert resp.json()["total"] == 1


@pytest.mark.django_db
def test_request_log_endpoints(client, account, api_key):
    _post(client, api_key)
    lst = client.get("/api/v1/request-logs", HTTP_X_API_KEY=api_key)
    assert lst.status_code == 200
    rows = lst.json()["data"]
    assert any(r["path"] == MESSAGES_URL for r in rows)

    rid = next(r["id"] for r in rows if r["path"] == MESSAGES_URL)
    detail = client.get(f"/api/v1/request-logs/{rid}", HTTP_X_API_KEY=api_key)
    assert detail.status_code == 200
    assert detail.json()["method"] == "POST"


@pytest.mark.django_db
def test_unknown_message_returns_404_envelope(client, account, api_key):
    resp = client.get("/api/v1/messages/msg_missing", HTTP_X_API_KEY=api_key)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_deliverability_endpoint(client, account, api_key):
    from apps.logs.models import MessageStatsDaily
    from django.utils import timezone

    MessageStatsDaily.objects.create(
        account=account, domain=None, day=timezone.now().date(), key_mode="live",
        sent=200, delivered=195, bounced=2,
    )
    resp = client.get("/api/v1/deliverability", HTTP_X_API_KEY=api_key)
    assert resp.status_code == 200
    body = resp.json()
    assert 0 <= body["score"] <= 100
    assert body["grade"]
    assert isinstance(body["checks"], list)
    assert body["scope"] == "account"


@pytest.mark.django_db
def test_deliverability_unknown_domain_404(client, account, api_key):
    resp = client.get("/api/v1/deliverability?domain=nope.example", HTTP_X_API_KEY=api_key)
    assert resp.status_code == 404

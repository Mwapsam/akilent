import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailMessage

MESSAGES_URL = "/api/v1/messages"


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(
        slug="p", name="P", price_monthly=Decimal("10"),
        max_emails_per_month=1, email_apis=True, api_rate_per_min=0,
    )
    Subscription.objects.create(
        account=acc, plan=plan, status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )
    return acc


@pytest.fixture
def test_key(account):
    obj, raw = EmailApiKey.create_for_account(account, name="sandbox", mode="test")
    return raw


def _send(client, key, to, callbacks=None):
    resp = client.post(
        MESSAGES_URL,
        data=json.dumps({"from": "anything@unverified.example", "to": to,
                         "subject": "hi", "text": "yo"}),
        content_type="application/json",
        HTTP_X_API_KEY=key,
    )
    return resp


def test_test_key_has_prefix(test_key):
    assert test_key.startswith("ak_test_")


@pytest.mark.django_db
def test_sandbox_delivered_outcome(client, account, test_key, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = _send(client, test_key, "delivered@sandbox.akilent.test")
    assert resp.status_code == 202
    msg = EmailMessage.objects.get()
    assert msg.key_mode == "test"
    types = list(msg.events.values_list("type", flat=True))
    assert types == ["queued", "sent", "delivered"]
    assert msg.status == "delivered"


@pytest.mark.django_db
def test_sandbox_bounce_outcome(client, account, test_key, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        _send(client, test_key, "bounce@sandbox.akilent.test")
    msg = EmailMessage.objects.get()
    assert "bounced" in msg.events.values_list("type", flat=True)
    assert msg.status == "bounced"


@pytest.mark.django_db
def test_sandbox_does_not_consume_quota(client, account, test_key, django_capture_on_commit_callbacks):
    # plan cap is 1/month; three test sends must all succeed
    for _ in range(3):
        with django_capture_on_commit_callbacks(execute=True):
            r = _send(client, test_key, "delivered@sandbox.akilent.test")
        assert r.status_code == 202
    assert EmailMessage.objects.count() == 3


@pytest.mark.django_db
def test_sandbox_excluded_from_live_stats(client, account, test_key, django_capture_on_commit_callbacks):
    from apps.logs.models import MessageStatsDaily

    with django_capture_on_commit_callbacks(execute=True):
        _send(client, test_key, "delivered@sandbox.akilent.test")
    row = MessageStatsDaily.objects.get()
    assert row.key_mode == "test"

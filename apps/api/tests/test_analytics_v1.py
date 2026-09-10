from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailTemplate
from apps.logs.models import MessageStatsDaily


@pytest.fixture
def api_key(db):
    user = User.objects.create_user("o", "o@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"),
                               max_emails_per_month=100, email_apis=True, email_templates=True,
                               api_rate_per_min=0)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    k, raw = EmailApiKey.create_for_account(acc, name="k")
    return raw, acc


@pytest.mark.django_db
def test_analytics_group_by_day_and_totals(client, api_key):
    key, acc = api_key
    today = timezone.now().date()
    MessageStatsDaily.objects.create(account=acc, day=today, sent=10, delivered=9, opened=4, unique_opens=3)
    MessageStatsDaily.objects.create(account=acc, day=today - timedelta(days=1), sent=5, delivered=5)

    r = client.get("/api/v1/analytics?group_by=day", HTTP_X_API_KEY=key)
    assert r.status_code == 200
    body = r.json()
    assert body["totals"]["sent"] == 15
    assert body["totals"]["delivered"] == 14
    assert len(body["data"]) == 2


@pytest.mark.django_db
def test_analytics_group_by_template(client, api_key):
    key, acc = api_key
    t = EmailTemplate.objects.create(account=acc, name="T", slug="welcome", subject="s")
    MessageStatsDaily.objects.create(account=acc, day=timezone.now().date(), template=t, sent=7)

    r = client.get("/api/v1/analytics?group_by=template", HTTP_X_API_KEY=key)
    assert r.status_code == 200
    assert r.json()["data"][0]["group"] == "welcome"
    assert r.json()["data"][0]["sent"] == 7


@pytest.mark.django_db
def test_analytics_rejects_bad_group_by(client, api_key):
    key, _ = api_key
    r = client.get("/api/v1/analytics?group_by=bogus", HTTP_X_API_KEY=key)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


@pytest.mark.django_db
def test_changelog_feed(client, api_key):
    key, _ = api_key
    r = client.get("/api/v1/changelog", HTTP_X_API_KEY=key)
    assert r.status_code == 200
    entries = r.json()["data"]
    assert entries and "changes" in entries[0]
    assert all("type" in c and "summary" in c for e in entries for c in e["changes"])

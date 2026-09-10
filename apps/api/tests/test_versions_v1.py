import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailTemplate
from apps.email.services.versions import activate_version, snapshot_version


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
    k.scopes = ["messages:send", "templates:manage"]
    k.save(update_fields=["scopes"])
    return raw, acc


@pytest.mark.django_db
def test_snapshot_numbers_increment_and_activate_rolls_back(api_key):
    _, acc = api_key
    t = EmailTemplate.objects.create(account=acc, name="T", slug="t", subject="v1 subj")
    v1 = snapshot_version(t)
    t.subject = "v2 subj"
    t.save()
    v2 = snapshot_version(t)
    assert (v1.number, v2.number) == (1, 2)
    assert v2.is_active and not EmailTemplate.objects.get(pk=t.pk).versions.get(number=1).is_active

    activate_version(t, 1)
    t.refresh_from_db()
    assert t.subject == "v1 subj"
    assert t.versions.get(number=1).is_active
    assert not t.versions.get(number=2).is_active


@pytest.mark.django_db
def test_versions_list_and_activate_endpoints(client, api_key):
    key, acc = api_key
    t = EmailTemplate.objects.create(account=acc, name="T", slug="t", subject="first")
    snapshot_version(t)
    t.subject = "second"
    t.save()
    snapshot_version(t)

    lst = client.get("/api/v1/templates/t/versions", HTTP_X_API_KEY=key)
    assert lst.status_code == 200
    nums = [v["number"] for v in lst.json()["data"]]
    assert nums == [2, 1]

    act = client.post("/api/v1/templates/t/versions/1/activate", HTTP_X_API_KEY=key)
    assert act.status_code == 200
    assert act.json()["subject"] == "first"

    missing = client.post("/api/v1/templates/t/versions/99/activate", HTTP_X_API_KEY=key)
    assert missing.status_code == 404


@pytest.mark.django_db
def test_api_version_endpoint(client, api_key):
    key, _ = api_key
    r = client.get("/api/v1/version", HTTP_X_API_KEY=key)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "v1"
    assert "v1" in body["supported"]
    assert body["openapi"] == "/api/schema"

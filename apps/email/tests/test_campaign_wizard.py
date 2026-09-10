"""Bulk-campaign wizard web flow (templates/email/campaigns.html + views)."""
import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import BulkEmailCampaign, EmailDomain


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.fixture
def bulk_plan(account):
    plan = Plan.objects.create(
        slug="p", name="P", price_monthly=Decimal("10"),
        max_emails_per_month=1000, email_apis=True, bulk_email=True,
        max_bulk_recipients_per_campaign=500,
    )
    Subscription.objects.create(
        account=account, plan=plan, status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )
    return plan


@pytest.fixture
def verified_domain(account):
    return EmailDomain.objects.create(
        account=account, domain="mail.acme.com", status=EmailDomain.Status.VERIFIED,
    )


@pytest.mark.django_db
def test_sample_csv_download(client, account):
    client.force_login(account.owner)
    resp = client.get("/email/campaigns/sample.csv")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "text/csv"
    assert "attachment" in resp["Content-Disposition"]
    assert b"to,first_name" in resp.content


@pytest.mark.django_db
def test_list_page_exposes_verified_domain_to_wizard(client, account, bulk_plan, verified_domain):
    client.force_login(account.owner)
    resp = client.get("/email/campaigns/")
    assert resp.status_code == 200
    assert b"campaign-wizard-config" in resp.content
    assert b"mail.acme.com" in resp.content


@pytest.mark.django_db
def test_create_from_pasted_addresses_queues_campaign(client, account, bulk_plan, verified_domain):
    client.force_login(account.owner)
    resp = client.post("/email/campaigns/create/", {
        "mode": "text",
        "from_email": "hello@mail.acme.com",
        "subject": "Hello",
        "text_body": "Hi everyone",
        "recipients_text": "ada@example.com, grace@example.com\nada@example.com",
    })
    assert resp.status_code == 302
    campaign = BulkEmailCampaign.objects.get(account=account)
    assert campaign.from_email == "hello@mail.acme.com"
    # de-duplicated to two unique recipients
    assert campaign.recipient_count == 2
    assert campaign.recipients.count() == 2


@pytest.mark.django_db
def test_schedule_toggle_defers_the_campaign(client, account, bulk_plan, verified_domain):
    from datetime import timedelta

    from apps.scheduler.models import ScheduledJob

    client.force_login(account.owner)
    when = (timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    resp = client.post("/email/campaigns/create/", {
        "mode": "text",
        "from_email": "hello@mail.acme.com",
        "subject": "Later",
        "text_body": "Hi",
        "recipients_text": "ada@example.com",
        "schedule_enabled": "1",
        "scheduled_at": when,
        "schedule_timezone": "UTC",
    })
    assert resp.status_code == 302
    campaign = BulkEmailCampaign.objects.get(account=account)
    assert campaign.status == BulkEmailCampaign.Status.SCHEDULED
    assert campaign.recipients.count() == 1
    job = ScheduledJob.objects.get(account=account)
    assert job.kind == ScheduledJob.Kind.EMAIL_CAMPAIGN
    assert job.target_campaign_id == campaign.id


@pytest.mark.django_db
def test_invalid_submission_rerenders_with_input_preserved(client, account, bulk_plan, verified_domain):
    client.force_login(account.owner)
    resp = client.post("/email/campaigns/create/", {
        "mode": "text",
        "from_email": "hello@mail.acme.com",
        "subject": "My subject line",
        "text_body": "Body copy",
        "recipients_text": "",  # nothing -> error
    })
    assert resp.status_code == 400
    assert not BulkEmailCampaign.objects.filter(account=account).exists()
    body = resp.content.decode()
    assert "My subject line" in body  # typed value survived
    assert "Add at least one recipient" in body


@pytest.mark.django_db
def test_send_test_uses_logged_in_user_address(client, account, bulk_plan, verified_domain, monkeypatch):
    sent = {}

    def fake_send(**kwargs):
        sent.update(kwargs)
        from types import SimpleNamespace
        return SimpleNamespace(id=1, status="queued")

    monkeypatch.setattr("apps.api.services.create_and_queue_message", fake_send)
    client.force_login(account.owner)

    resp = client.post(
        "/email/campaigns/send-test/",
        data=json.dumps({
            "from_email": "hello@mail.acme.com",
            "subject": "Preview me",
            "text_body": "Hi {{ first_name }}",
            "variables": {"first_name": "Ada"},
        }),
        content_type="application/json",
    )
    assert resp.status_code == 202
    assert sent["to_email"] == "owner@example.com"
    assert sent["from_email"] == "hello@mail.acme.com"


@pytest.mark.django_db
def test_send_test_requires_content(client, account, bulk_plan, verified_domain):
    client.force_login(account.owner)
    resp = client.post(
        "/email/campaigns/send-test/",
        data=json.dumps({"from_email": "hello@mail.acme.com"}),
        content_type="application/json",
    )
    assert resp.status_code == 400
    assert "subject" in resp.json()["error"].lower()

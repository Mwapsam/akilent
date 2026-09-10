from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import EmailApiKey, EmailDomain, EmailMessage
from apps.scheduler.models import ScheduledJob

MESSAGES_URL = "/api/v1/messages"
JOBS_URL = "/api/v1/scheduled-jobs"


@pytest.fixture
def api_key(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(
        slug="p", name="P", price_monthly=Decimal("10"),
        max_emails_per_month=1000, email_apis=True, bulk_email=True,
        api_rate_per_min=0, max_bulk_recipients_per_campaign=-1,
    )
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    EmailDomain.objects.create(account=acc, domain="mail.acme.com",
                               status=EmailDomain.Status.VERIFIED)
    _, raw = EmailApiKey.create_for_account(acc, name="k",
                                            scopes=["messages:send", "messages:send:bulk"])
    return raw, acc


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


@pytest.mark.django_db
def test_scheduled_message_returns_job_not_send(client, api_key):
    key, acc = api_key
    when = timezone.now() + timedelta(hours=5)
    r = client.post(MESSAGES_URL, data={
        "from": "hi@mail.acme.com", "to": "x@example.com",
        "subject": "s", "text": "t", "scheduled_at": _iso(when),
    }, content_type="application/json", HTTP_X_API_KEY=key)
    assert r.status_code == 202, r.content
    body = r.json()
    assert body["status"] == "scheduled"
    assert body["scheduled_job"]["id"].startswith("job_")
    assert EmailMessage.objects.count() == 0
    assert ScheduledJob.objects.filter(account=acc, status="scheduled").count() == 1


@pytest.mark.django_db
def test_past_scheduled_at_is_400(client, api_key):
    key, _ = api_key
    r = client.post(MESSAGES_URL, data={
        "from": "hi@mail.acme.com", "to": "x@example.com", "text": "t",
        "scheduled_at": _iso(timezone.now() - timedelta(hours=1)),
    }, content_type="application/json", HTTP_X_API_KEY=key)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_schedule"


@pytest.mark.django_db
def test_list_get_patch_delete_job(client, api_key):
    key, acc = api_key
    when = timezone.now() + timedelta(hours=5)
    pid = client.post(MESSAGES_URL, data={
        "from": "hi@mail.acme.com", "to": "x@example.com", "text": "t",
        "scheduled_at": _iso(when),
    }, content_type="application/json", HTTP_X_API_KEY=key).json()["scheduled_job"]["id"]

    lst = client.get(JOBS_URL, HTTP_X_API_KEY=key).json()
    assert lst["total"] == 1 and lst["data"][0]["id"] == pid

    later = timezone.now() + timedelta(days=3)
    patched = client.patch(f"{JOBS_URL}/{pid}", data={"scheduled_at": _iso(later)},
                           content_type="application/json", HTTP_X_API_KEY=key)
    assert patched.status_code == 200

    deleted = client.delete(f"{JOBS_URL}/{pid}", HTTP_X_API_KEY=key)
    assert deleted.status_code == 200
    assert ScheduledJob.objects.get(public_id=pid).status == "cancelled"


@pytest.mark.django_db
def test_patch_after_cancel_is_409(client, api_key):
    key, _ = api_key
    when = timezone.now() + timedelta(hours=5)
    pid = client.post(MESSAGES_URL, data={
        "from": "hi@mail.acme.com", "to": "x@example.com", "text": "t",
        "scheduled_at": _iso(when),
    }, content_type="application/json", HTTP_X_API_KEY=key).json()["scheduled_job"]["id"]
    client.delete(f"{JOBS_URL}/{pid}", HTTP_X_API_KEY=key)

    r = client.patch(f"{JOBS_URL}/{pid}",
                     data={"scheduled_at": _iso(timezone.now() + timedelta(days=1))},
                     content_type="application/json", HTTP_X_API_KEY=key)
    assert r.status_code == 409


@pytest.mark.django_db
def test_account_isolation(client, api_key):
    key, _ = api_key
    other = Account.objects.create(company_name="Other")
    plan = Plan.objects.create(slug="o", name="O", price_monthly=Decimal("1"),
                               email_apis=True, api_rate_per_min=0)
    Subscription.objects.create(account=other, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    job = ScheduledJob.objects.create(
        account=other, kind=ScheduledJob.Kind.EMAIL_SINGLE,
        fire_at=timezone.now() + timedelta(hours=1), idempotency_key="k1",
    )
    r = client.get(f"{JOBS_URL}/{job.public_id}", HTTP_X_API_KEY=key)
    assert r.status_code == 404


@pytest.mark.django_db
def test_immediate_send_still_works(client, api_key):
    key, _ = api_key
    r = client.post(MESSAGES_URL, data={
        "from": "hi@mail.acme.com", "to": "x@example.com", "text": "t",
    }, content_type="application/json", HTTP_X_API_KEY=key)
    assert r.status_code == 202
    assert r.json()["status"] != "scheduled"
    assert EmailMessage.objects.count() == 1

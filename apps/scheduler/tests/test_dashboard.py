from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Membership
from apps.scheduler.models import ScheduledJob


@pytest.fixture
def logged_in(client, account):
    m = Membership.objects.filter(account=account).first()
    client.force_login(m.user)
    return client, account


@pytest.mark.django_db
def test_index_lists_upcoming_grouped(logged_in):
    client, account = logged_in
    ScheduledJob.objects.create(
        account=account, kind=ScheduledJob.Kind.EMAIL_SINGLE,
        fire_at=timezone.now() + timedelta(hours=2), idempotency_key="a",
    )
    r = client.get("/scheduled/")
    assert r.status_code == 200
    assert b"Scheduled" in r.content


@pytest.mark.django_db
def test_cancel_endpoint(logged_in):
    client, account = logged_in
    job = ScheduledJob.objects.create(
        account=account, kind=ScheduledJob.Kind.EMAIL_SINGLE,
        fire_at=timezone.now() + timedelta(hours=2), idempotency_key="b",
    )
    r = client.post(f"/scheduled/{job.public_id}/cancel/")
    assert r.status_code == 200
    job.refresh_from_db()
    assert job.status == ScheduledJob.Status.CANCELLED


@pytest.mark.django_db
def test_cannot_cancel_other_accounts_job(client, account, db):
    from apps.accounts.models import Account
    from django.contrib.auth.models import User

    other_user = User.objects.create_user("stranger", "s@example.com", "pw")
    Account.objects.create(company_name="Stranger")
    client.force_login(other_user)
    job = ScheduledJob.objects.create(
        account=account, kind=ScheduledJob.Kind.EMAIL_SINGLE,
        fire_at=timezone.now() + timedelta(hours=2), idempotency_key="c",
    )
    r = client.post(f"/scheduled/{job.public_id}/cancel/")
    assert r.status_code in (403, 404)
    job.refresh_from_db()
    assert job.status == ScheduledJob.Status.SCHEDULED

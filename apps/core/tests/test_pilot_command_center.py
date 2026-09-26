"""Pilot Command Center: staff only, one row per business on WhatsApp, late queues stand out."""
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.core.tasks import KEY, heartbeat, send_heartbeats
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

URL = "/manage/pilot/"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()


@pytest.fixture
def staff(client, db):
    client.force_login(User.objects.create_superuser("root", "root@example.com", "pw"))
    return client


@pytest.mark.django_db
def test_only_staff_can_open_it(client):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    assert client.get(URL).status_code in (302, 403)


@pytest.mark.django_db
def test_lists_only_businesses_with_whatsapp(staff):
    on = Account.objects.create(company_name="Mwamba Kitchen")
    Account.objects.create(company_name="Email Only Ltd")
    WhatsAppBusinessNumber.objects.create(account=on, phone_number_id="PN1", waba_id="W", access_token="t",
                                          is_active=True)
    html = staff.get(URL).content.decode()
    assert "Mwamba Kitchen" in html and "Email Only Ltd" not in html
    assert "Quiet" in html, "no customer messages yet counts as quiet"
    assert "Measuring, day 1 of 7" in html


@pytest.mark.django_db
def test_a_queue_without_a_recent_heartbeat_is_late(staff, settings):
    settings.WORKER_QUEUES = ["celery", "scheduler"]
    heartbeat("celery")
    cache.set(KEY.format("scheduler"), (timezone.now() - timedelta(minutes=9)).isoformat())
    html = staff.get(URL).content.decode()
    assert "Late · 9" in html and html.count("Late ·") == 1


@pytest.mark.django_db
def test_heartbeats_go_through_every_queue(settings, monkeypatch):
    settings.WORKER_QUEUES = ["celery", "ai"]
    sent = []
    monkeypatch.setattr(heartbeat, "apply_async", lambda args, queue, expires: sent.append(queue))
    send_heartbeats()
    assert sent == ["celery", "ai"]


@pytest.mark.django_db
def test_time_to_first_value_counts_from_the_first_number_and_ignores_earlier_runs(staff):
    from apps.automation.api import adoption
    from apps.automation.models import Workflow, WorkflowRun
    from apps.contacts.models import Contact
    from apps.whatsapp.api import connected_since

    account = Account.objects.create(company_name="Mwamba Kitchen")
    now = timezone.now()
    old = WhatsAppBusinessNumber.objects.create(account=account, phone_number_id="OLD", waba_id="W",
                                                access_token="t", is_active=False)
    new = WhatsAppBusinessNumber.objects.create(account=account, phone_number_id="NEW", waba_id="W",
                                                access_token="t", is_active=True)
    WhatsAppBusinessNumber.objects.filter(pk=old.pk).update(created_at=now - timedelta(days=20))
    WhatsAppBusinessNumber.objects.filter(pk=new.pk).update(created_at=now - timedelta(days=5))
    connected = connected_since(account)
    assert connected == now - timedelta(days=20), "a replaced number doesn't reset the connection date"

    wf = Workflow.objects.create(account=account, name="Welcome", status=Workflow.Status.PUBLISHED)
    contact = Contact.objects.create(account=account, phone="+260971234567")
    before = WorkflowRun.objects.create(workflow=wf, contact=contact, subject_key="a")
    after = WorkflowRun.objects.create(workflow=wf, contact=contact, subject_key="b")
    WorkflowRun.objects.filter(pk=before.pk).update(started_at=now - timedelta(days=30))
    WorkflowRun.objects.filter(pk=after.pk).update(started_at=now - timedelta(days=18))
    assert adoption(account, since=connected)["first_run_at"] == now - timedelta(days=18)

    html = staff.get(URL).content.decode()
    assert "2 days after connecting" in html


@pytest.mark.django_db
def test_suggestions_count_only_what_the_model_produced():
    from apps.ai.api import usage_summary
    from apps.ai.models import AIProposal

    account = Account.objects.create(company_name="Acme")
    now = timezone.now()
    base = dict(account=account)
    for status, ready in [("used", True), ("expired", True), ("expired", False), ("error", False)]:
        AIProposal.objects.create(**base, status=status, ready_at=now if ready else None)
    summary = usage_summary(account, since=now - timedelta(days=1))
    assert summary == {"suggested": 2, "used": 1, "errors": 1}


@pytest.mark.django_db
def test_backups_say_not_configured_without_a_bucket(staff, settings):
    settings.BACKUP_S3_BUCKET = ""
    assert "Not configured" in staff.get(URL).content.decode()

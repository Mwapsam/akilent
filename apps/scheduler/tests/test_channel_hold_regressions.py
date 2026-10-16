"""Regression tests for channel switch-off: held sends, recurring skips, and starvation."""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.api.services import create_and_queue_message
from apps.core import channels as ch
from apps.scheduler import drainer
from apps.scheduler.drainer import _claim_due, drain
from apps.scheduler.models import ScheduledJob


@pytest.fixture(autouse=True)
def _fresh_channel_cache():
    reset = {"value": None, "loaded_at": 0.0, "failed_at": 0.0}
    ch._cache.update(reset)
    yield
    ch._cache.update(reset)


@pytest.fixture
def operator(db):
    from django.contrib.auth.models import User

    return User.objects.create_superuser("ops", "ops@example.com", "pw-123456!")


def _switch(channel, enabled, operator):
    ch.set_channel_enabled(channel, enabled, request=SimpleNamespace(user=operator))


def _msg(**over):
    body = dict(
        from_email="hello@mail.acme.com",
        to_email="rcpt@example.com",
        subject="Hi",
        text_body="Hello",
    )
    body.update(over)
    return body


# --- Recurring jobs: a late occurrence is skipped; the series is not terminated -----------


@pytest.mark.django_db
def test_late_recurring_occurrence_is_skipped_and_series_continues(
    account, verified_domain, future
):
    job = create_and_queue_message(
        account=account,
        scheduled_at=future(hours=1),
        tz="UTC",
        recurrence="FREQ=DAILY",
        **_msg(),
    )
    job.fire_at = timezone.now() - timedelta(days=2)  # far past the stale grace
    job.stale_grace_secs = 60
    job.save(update_fields=["fire_at", "stale_grace_secs"])

    drain()
    job.refresh_from_db()
    assert job.status == ScheduledJob.Status.SCHEDULED
    assert (
        job.fire_at > timezone.now()
    )  # next occurrence after now, no backlog of skips


@pytest.mark.django_db
def test_late_one_off_job_is_still_failed(account, verified_domain, future):
    job = create_and_queue_message(
        account=account, scheduled_at=future(hours=1), tz="UTC", **_msg()
    )
    job.fire_at = timezone.now() - timedelta(days=2)
    job.stale_grace_secs = 60
    job.save(update_fields=["fire_at", "stale_grace_secs"])

    drain()
    job.refresh_from_db()
    assert job.status == ScheduledJob.Status.FAILED
    assert job.recurrence == ""


# --- Held jobs must not starve the batch ---------------------------------------------------


@pytest.mark.django_db
def test_jobs_on_a_switched_off_channel_do_not_use_up_the_batch(operator):
    account = Account.objects.create(company_name="Acme")
    past = timezone.now() - timedelta(minutes=5)
    for i in range(3):  # WhatsApp jobs, due earliest
        ScheduledJob.objects.create(
            account=account,
            kind=ScheduledJob.Kind.WHATSAPP,
            status=ScheduledJob.Status.SCHEDULED,
            fire_at=past - timedelta(minutes=10 - i),
            idempotency_key=f"wa-{i}",
        )
    email_job = ScheduledJob.objects.create(
        account=account,
        kind=ScheduledJob.Kind.EMAIL_SINGLE,
        status=ScheduledJob.Status.SCHEDULED,
        fire_at=past,
        idempotency_key="email-1",
    )
    _switch("whatsapp", False, operator)

    with mock.patch.object(drainer, "BATCH", 2):
        claimed = _claim_due(timezone.now())

    assert claimed == [email_job.pk]


# --- Email: stays queued while off, and sends again once it is on -------------------------


@pytest.mark.django_db
def test_queued_email_is_retried_not_failed_while_email_is_off(operator):
    from apps.email.tasks import _send_email_message

    _switch("email", False, operator)
    task = mock.Mock()
    task.retry.side_effect = RuntimeError("retry scheduled")
    msg = mock.Mock()

    with pytest.raises(RuntimeError):
        _send_email_message(task, msg, "text", "")

    msg.mark_failed.assert_not_called()
    kwargs = task.retry.call_args.kwargs
    assert kwargs["countdown"] <= 1800
    assert kwargs["max_retries"] >= 1000


# --- Instagram: no send while switched off -------------------------------------------------


@pytest.mark.django_db
def test_instagram_send_is_blocked_while_switched_off(operator):
    from apps.instagram.services.outbound import send_outbound

    _switch("instagram", False, operator)
    outbound = mock.Mock(status="queued", pk=0)
    assert send_outbound(outbound) is False
    outbound.mark_failed.assert_not_called()


# --- WhatsApp campaigns: retry stays inside the RabbitMQ consumer timeout ------------------


@pytest.mark.django_db
def test_campaign_retry_while_whatsapp_is_off_stays_within_consumer_timeout(operator):
    from apps.whatsapp.campaigns import send_campaign

    _switch("whatsapp", False, operator)
    with mock.patch.object(
        send_campaign, "retry", side_effect=RuntimeError("retry")
    ) as retry:
        with pytest.raises(RuntimeError):
            send_campaign(1)
    assert retry.call_args.kwargs["countdown"] <= 1800


# --- API: a send on a switched-off channel reaches the view (403), not a 404 ---------------


@pytest.mark.django_db
def test_api_send_reaches_the_view_when_channel_is_switched_off(client, operator):
    _switch("email", False, operator)
    response = client.post(
        "/api/v1/messages/", data={}, content_type="application/json"
    )
    assert "is not enabled" not in response.content.decode()


# --- Dashboard: with Email off, the aggregates are zero with the same keys -----------------


@pytest.mark.django_db
def test_email_stats_are_zero_with_the_same_keys_when_email_is_off(operator):
    from apps.accounts.views import _email_stats

    _switch("email", False, operator)
    stats = _email_stats(account=None, subscription=None)
    assert set(stats) == {
        "sent_month",
        "failed_month",
        "success_rate_display",
        "failed_sub",
        "domains_verified",
        "domains_sub",
        "emails_used",
        "email_quota",
        "usage_pct",
        "recent_sends",
    }
    assert stats["sent_month"] == 0
    assert stats["recent_sends"] == []

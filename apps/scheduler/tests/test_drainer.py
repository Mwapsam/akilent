from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.api.services import create_and_queue_campaign, create_and_queue_message
from apps.scheduler.api import SchedulingError, cancel_job, reschedule_job
from apps.scheduler.drainer import drain
from apps.scheduler.models import ScheduledJob
from apps.email.models import BulkEmailCampaign, EmailMessage


def _msg_payload(**over):
    body = dict(
        from_email="hello@mail.acme.com",
        to_email="rcpt@example.com",
        subject="Hi",
        text_body="Hello",
    )
    body.update(over)
    return body


# --- scheduling (no send now) -------------------------------------------

@pytest.mark.django_db
def test_future_message_creates_job_not_message(account, verified_domain, future):
    job = create_and_queue_message(
        account=account, scheduled_at=future(hours=2), tz="UTC", **_msg_payload()
    )
    assert isinstance(job, ScheduledJob)
    assert job.status == ScheduledJob.Status.SCHEDULED
    assert job.kind == ScheduledJob.Kind.EMAIL_SINGLE
    assert EmailMessage.objects.count() == 0


@pytest.mark.django_db
def test_split_timezone_form_resolves_to_utc(account, verified_domain):
    from datetime import datetime

    # A summer date within the 1-year scheduling horizon: 09:00 EDT -> 13:00 UTC.
    year = timezone.now().year + 1
    naive = datetime(year, 7, 1, 9, 0)
    job = create_and_queue_message(
        account=account, scheduled_at=naive, tz="America/New_York", **_msg_payload()
    )
    assert job.fire_at.hour == 13
    assert job.tz == "America/New_York"


@pytest.mark.django_db
def test_past_time_rejected(account, verified_domain):
    with pytest.raises(SchedulingError):
        create_and_queue_message(
            account=account,
            scheduled_at=timezone.now() - timedelta(minutes=5),
            tz="UTC", **_msg_payload()
        )


@pytest.mark.django_db
def test_within_min_lead_rejected(account, verified_domain):
    with pytest.raises(SchedulingError):
        create_and_queue_message(
            account=account,
            scheduled_at=timezone.now() + timedelta(seconds=10),
            tz="UTC", **_msg_payload()
        )


@pytest.mark.django_db
def test_unverified_domain_rejected_at_schedule_time(account, future):
    from apps.email.exceptions import UnverifiedDomainError

    with pytest.raises(UnverifiedDomainError):
        create_and_queue_message(
            account=account, scheduled_at=future(hours=1), tz="UTC", **_msg_payload()
        )
    assert ScheduledJob.objects.count() == 0


# --- drain -> send ----------------------------------------------------

@pytest.mark.django_db
def test_drain_fires_due_message(account, verified_domain, future):
    job = create_and_queue_message(
        account=account, scheduled_at=future(hours=1), tz="UTC", **_msg_payload()
    )
    job.fire_at = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=["fire_at"])

    tally = drain()

    job.refresh_from_db()
    assert tally["fired"] == 1
    assert job.status == ScheduledJob.Status.SENT
    assert EmailMessage.objects.count() == 1
    assert job.target_message_id is not None
    assert job.result["message_public_id"].startswith("msg_")


@pytest.mark.django_db
def test_drain_is_idempotent_across_two_ticks(account, verified_domain, future):
    job = create_and_queue_message(
        account=account, scheduled_at=future(hours=1), tz="UTC", **_msg_payload()
    )
    job.fire_at = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=["fire_at"])

    drain()
    drain()

    assert EmailMessage.objects.count() == 1


@pytest.mark.django_db
def test_too_stale_job_is_failed_not_sent(account, verified_domain, future):
    job = create_and_queue_message(
        account=account, scheduled_at=future(hours=1), tz="UTC", **_msg_payload()
    )
    job.fire_at = timezone.now() - timedelta(hours=7)  # > 6h grace
    job.save(update_fields=["fire_at"])

    tally = drain()

    job.refresh_from_db()
    assert tally["stale_skipped"] == 1
    assert job.status == ScheduledJob.Status.FAILED
    assert "missed schedule window" in job.error
    assert EmailMessage.objects.count() == 0


# --- campaigns -------------------------------------------------------

@pytest.mark.django_db
def test_future_campaign_is_scheduled_with_audience_visible(account, verified_domain, future):
    with patch("apps.email.tasks.dispatch_campaign.delay") as dispatch:
        job = create_and_queue_campaign(
            account=account,
            from_email="hello@mail.acme.com",
            subject="s", text_body="t",
            recipients=[{"to": "a@example.com"}, {"to": "b@example.com"}],
            scheduled_at=future(hours=3), tz="UTC",
        )
    assert isinstance(job, ScheduledJob)
    camp = job.target_campaign
    assert camp.status == BulkEmailCampaign.Status.SCHEDULED
    assert camp.recipient_count == 2
    assert camp.recipients.count() == 2
    dispatch.assert_not_called()


@pytest.mark.django_db
def test_drain_flips_campaign_to_queued_and_dispatches(account, verified_domain, future):
    with patch("apps.email.tasks.dispatch_campaign.delay") as dispatch:
        job = create_and_queue_campaign(
            account=account,
            from_email="hello@mail.acme.com",
            subject="s", text_body="t",
            recipients=[{"to": "a@example.com"}],
            scheduled_at=future(hours=1), tz="UTC",
        )
        job.fire_at = timezone.now() - timedelta(seconds=1)
        job.save(update_fields=["fire_at"])
        drain()

    job.refresh_from_db()
    job.target_campaign.refresh_from_db()
    assert job.status == ScheduledJob.Status.RUNNING
    assert job.target_campaign.status == BulkEmailCampaign.Status.QUEUED
    dispatch.assert_called_once_with(job.target_campaign_id)


# --- cancel / reschedule -------------------------------------------

@pytest.mark.django_db
def test_cancel_while_scheduled_cascades_to_campaign(account, verified_domain, future):
    with patch("apps.email.tasks.dispatch_campaign.delay"):
        job = create_and_queue_campaign(
            account=account, from_email="hello@mail.acme.com", subject="s", text_body="t",
            recipients=[{"to": "a@example.com"}], scheduled_at=future(hours=2), tz="UTC",
        )
    cancel_job(job)
    job.refresh_from_db()
    job.target_campaign.refresh_from_db()
    assert job.status == ScheduledJob.Status.CANCELLED
    assert job.target_campaign.status == BulkEmailCampaign.Status.CANCELLED
    assert drain()["due"] == 0


@pytest.mark.django_db
def test_cancel_after_sent_is_rejected(account, verified_domain, future):
    job = create_and_queue_message(
        account=account, scheduled_at=future(hours=1), tz="UTC", **_msg_payload()
    )
    job.fire_at = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=["fire_at"])
    drain()
    job.refresh_from_db()
    with pytest.raises(SchedulingError):
        cancel_job(job)


@pytest.mark.django_db
def test_reschedule_moves_fire_at_and_resets_attempts(account, verified_domain, future):
    job = create_and_queue_message(
        account=account, scheduled_at=future(hours=1), tz="UTC", **_msg_payload()
    )
    job.attempts = 3
    job.save(update_fields=["attempts"])
    reschedule_job(job, scheduled_at=future(days=2), tz="Europe/London")
    job.refresh_from_db()
    assert job.attempts == 0
    assert job.tz == "Europe/London"
    assert job.fire_at > timezone.now() + timedelta(days=1)


# --- recurrence ----------------------------------------------------

@pytest.mark.django_db
def test_recurring_job_rearms_after_fire(account, verified_domain, future):
    job = create_and_queue_message(
        account=account, scheduled_at=future(hours=1), tz="UTC",
        recurrence="FREQ=DAILY;BYHOUR=9;BYMINUTE=0", **_msg_payload()
    )
    first_fire = job.fire_at
    job.fire_at = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=["fire_at"])

    drain()

    job.refresh_from_db()
    assert job.status == ScheduledJob.Status.SCHEDULED  # re-armed
    assert job.occurrence_count == 1
    assert job.fire_at > first_fire
    assert EmailMessage.objects.count() == 1


@pytest.mark.django_db
def test_sub_hour_recurrence_rejected(account, verified_domain, future):
    with pytest.raises(SchedulingError):
        create_and_queue_message(
            account=account, scheduled_at=future(hours=1), tz="UTC",
            recurrence="FREQ=MINUTELY;INTERVAL=10", **_msg_payload()
        )

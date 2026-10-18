"""Webhook events stored but never processed are re-enqueued, a bounded number of times."""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from apps.core import webhook_replay


@pytest.fixture(autouse=True)
def _channels_on():
    with (
        mock.patch.object(webhook_replay, "channel_state", return_value="enabled"),
        mock.patch.object(webhook_replay, "invalidate_channel_cache"),
    ):
        yield


def _wa_event(*, age_minutes=30, **fields):
    from apps.whatsapp.models import WebhookEventLog

    event = WebhookEventLog.objects.create(
        source=WebhookEventLog.Source.WHATSAPP,
        event_type="message",
        payload={},
        **fields,
    )
    WebhookEventLog.objects.filter(pk=event.pk).update(
        created_at=timezone.now() - timedelta(minutes=age_minutes)
    )
    return event


@pytest.mark.django_db
def test_old_unprocessed_whatsapp_event_is_re_enqueued():
    stuck = _wa_event()
    _wa_event(processed=True)
    _wa_event(held=True)
    _wa_event(age_minutes=1)  # may still be on its way through the worker
    _wa_event(attempts=webhook_replay._MAX_SWEEP_ATTEMPTS)  # given up on
    with mock.patch("apps.whatsapp.tasks.process_whatsapp_event.delay") as delay:
        result = webhook_replay.sweep_stuck_webhooks()
    delay.assert_called_once_with(stuck.pk)
    assert result["whatsapp"] == 1


@pytest.mark.django_db
def test_failed_instagram_event_is_re_enqueued_but_unresolved_ones_are_not():
    from apps.instagram.models.webhook import WebhookEventLog
    from apps.instagram.tests.helpers import make_account, make_instagram_account

    account, _ = make_account()
    iba = make_instagram_account(account)
    failed = WebhookEventLog.objects.create(
        instagram_account=iba,
        event_id="e1",
        event_type="message",
        raw_payload={},
        status=WebhookEventLog.Status.FAILED,
        attempts=4,
    )
    WebhookEventLog.objects.create(
        instagram_account=None,
        event_id="e2",
        event_type="message",
        raw_payload={},
        status=WebhookEventLog.Status.RECEIVED,
    )
    WebhookEventLog.objects.update(received_at=timezone.now() - timedelta(hours=1))
    with mock.patch("apps.instagram.tasks.process_instagram_event.delay") as delay:
        webhook_replay.sweep_stuck_webhooks()
    delay.assert_called_once_with(failed.pk)


@pytest.mark.django_db
def test_broker_outage_stops_the_sweep_without_raising():
    _wa_event()
    _wa_event()
    with mock.patch(
        "apps.whatsapp.tasks.process_whatsapp_event.delay", side_effect=OSError
    ) as delay:
        result = webhook_replay.sweep_stuck_webhooks()
    assert delay.call_count == 1 and result["whatsapp"] == 0


@pytest.mark.django_db
def test_old_webhook_bodies_are_pruned_but_held_and_recent_ones_kept():
    from apps.whatsapp.models import WebhookEventLog

    old_done = _wa_event(age_minutes=31 * 24 * 60, processed=True)
    old_failed = _wa_event(age_minutes=31 * 24 * 60)  # unprocessed: kept 90 days
    ancient_failed = _wa_event(age_minutes=91 * 24 * 60)
    held = _wa_event(age_minutes=200 * 24 * 60, held=True)
    recent = _wa_event(age_minutes=60, processed=True)

    result = webhook_replay.prune_webhook_logs()

    remaining = set(WebhookEventLog.objects.values_list("pk", flat=True))
    assert remaining == {old_failed.pk, held.pk, recent.pk}
    assert old_done.pk not in remaining and ancient_failed.pk not in remaining
    assert result["whatsapp"] == 2


@pytest.mark.django_db
def test_old_processed_instagram_events_are_pruned():
    from apps.instagram.models.webhook import WebhookEventLog

    WebhookEventLog.objects.create(
        event_id="old",
        event_type="message",
        raw_payload={},
        status=WebhookEventLog.Status.PROCESSED,
    )
    WebhookEventLog.objects.create(
        event_id="new",
        event_type="message",
        raw_payload={},
        status=WebhookEventLog.Status.PROCESSED,
    )
    WebhookEventLog.objects.filter(event_id="old").update(
        received_at=timezone.now() - timedelta(days=31)
    )
    webhook_replay.prune_webhook_logs()
    assert list(WebhookEventLog.objects.values_list("event_id", flat=True)) == ["new"]

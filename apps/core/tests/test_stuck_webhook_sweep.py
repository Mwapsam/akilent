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

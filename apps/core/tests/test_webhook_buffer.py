"""Inbound webhooks received while their channel is switched off are stored, then replayed."""

import hashlib
import hmac
import json
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.db import OperationalError

from apps.core import channels as ch
from apps.core.webhook_replay import replay_held_webhooks

SECRET = "app-secret"


@pytest.fixture(autouse=True)
def _fresh_channel_cache():
    reset = {"value": None, "loaded_at": 0.0, "failed_at": 0.0}
    ch._cache.update(reset)
    yield
    ch._cache.update(reset)


@pytest.fixture(autouse=True)
def _no_background_replay():
    # Tests call replay_held_webhooks() themselves; switching a channel on must not start one.
    with mock.patch.object(ch, "_kick_webhook_replay"):
        yield


@pytest.fixture
def wired(settings):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    settings.WHATSAPP_APP_SECRET = SECRET
    settings.WHATSAPP_VERIFY_TOKEN = "verify"


@pytest.fixture
def operator(db):
    return User.objects.create_superuser("ops", "ops@example.com", "pw-123456!")


def _request(user):
    from types import SimpleNamespace

    return SimpleNamespace(user=user)


def _switch(channel, enabled, operator):
    ch.set_channel_enabled(channel, enabled, request=_request(operator))


def _signed(body: bytes) -> dict:
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"HTTP_X_HUB_SIGNATURE_256": f"sha256={sig}"}


def _wa_body(message_id: str) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": "260971000001",
                                    "type": "text",
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload).encode()


def _post_wa(client, body: bytes, **extra):
    return client.post(
        "/whatsapp/webhook/",
        data=body,
        content_type="application/json",
        **_signed(body),
        **extra,
    )


# --- WhatsApp: store while off -------------------------------------------------------------


@pytest.mark.django_db
def test_webhook_while_off_is_stored_held_and_acknowledged(client, wired, operator):
    from apps.whatsapp.models import WebhookEventLog

    _switch("whatsapp", False, operator)
    with mock.patch("apps.whatsapp.tasks.process_whatsapp_event.delay") as delay:
        response = _post_wa(client, _wa_body("wamid.1"))

    assert response.status_code == 200
    event = WebhookEventLog.objects.get()
    assert event.held is True
    assert event.processed is False
    delay.assert_not_called()


@pytest.mark.django_db
def test_unsigned_webhook_while_off_is_refused_and_stores_nothing(
    client, wired, operator
):
    from apps.whatsapp.models import WebhookEventLog

    _switch("whatsapp", False, operator)
    response = client.post(
        "/whatsapp/webhook/", data=_wa_body("wamid.2"), content_type="application/json"
    )
    assert response.status_code == 403
    assert not WebhookEventLog.objects.exists()


@pytest.mark.django_db
def test_duplicate_delivery_while_off_is_stored_once(client, wired, operator):
    from apps.whatsapp.models import WebhookEventLog

    _switch("whatsapp", False, operator)
    body = _wa_body("wamid.3")
    assert _post_wa(client, body).status_code == 200
    assert _post_wa(client, body).status_code == 200
    assert WebhookEventLog.objects.count() == 1


@pytest.mark.django_db
def test_database_error_during_persistence_is_not_acknowledged(client, wired, operator):
    """No 200 unless the event is stored: Meta must retry if we could not keep it."""
    from apps.whatsapp.models import WebhookEventLog

    _switch("whatsapp", False, operator)
    with mock.patch.object(
        WebhookEventLog.objects, "create", side_effect=OperationalError("db down")
    ):
        with pytest.raises(OperationalError):
            _post_wa(client, _wa_body("wamid.4"))


@pytest.mark.django_db
def test_webhook_while_on_is_processed_immediately(
    client, wired, operator, django_capture_on_commit_callbacks
):
    from apps.whatsapp.models import WebhookEventLog

    with mock.patch("apps.whatsapp.tasks.process_whatsapp_event.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            response = _post_wa(client, _wa_body("wamid.5"))

    assert response.status_code == 200
    assert WebhookEventLog.objects.get().held is False
    delay.assert_called_once()


# --- Replay ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_replay_releases_held_events_oldest_first_and_only_once(wired, operator):
    from apps.whatsapp.models import WebhookEventLog

    _switch("whatsapp", False, operator)
    first = WebhookEventLog.objects.create(
        source=WebhookEventLog.Source.WHATSAPP,
        event_type="message",
        payload={"n": 1},
        held=True,
    )
    second = WebhookEventLog.objects.create(
        source=WebhookEventLog.Source.WHATSAPP,
        event_type="message",
        payload={"n": 2},
        held=True,
    )

    _switch("whatsapp", True, operator)
    with mock.patch("apps.whatsapp.tasks.process_whatsapp_event.delay") as delay:
        replay_held_webhooks()
        replay_held_webhooks()  # a second sweep must not release them again

    assert [c.args[0] for c in delay.call_args_list] == [first.pk, second.pk]
    first.refresh_from_db()
    assert first.held is False


@pytest.mark.django_db
def test_replay_does_nothing_while_still_off(wired, operator):
    from apps.whatsapp.models import WebhookEventLog

    _switch("whatsapp", False, operator)
    WebhookEventLog.objects.create(
        source=WebhookEventLog.Source.WHATSAPP,
        event_type="message",
        payload={},
        held=True,
    )
    with mock.patch("apps.whatsapp.tasks.process_whatsapp_event.delay") as delay:
        result = replay_held_webhooks()
    delay.assert_not_called()
    assert "whatsapp" not in result


@pytest.mark.django_db
def test_failed_enqueue_puts_the_event_back_on_hold(wired, operator):
    from apps.whatsapp.models import WebhookEventLog

    _switch("whatsapp", False, operator)
    event = WebhookEventLog.objects.create(
        source=WebhookEventLog.Source.WHATSAPP,
        event_type="message",
        payload={},
        held=True,
    )
    _switch("whatsapp", True, operator)
    with mock.patch(
        "apps.whatsapp.tasks.process_whatsapp_event.delay",
        side_effect=ConnectionError("broker down"),
    ):
        replay_held_webhooks()
    event.refresh_from_db()
    assert event.held is True


@pytest.mark.django_db
def test_broker_failure_mid_replay_keeps_the_rest_held(wired, operator):
    """A broker failure stops the sweep; later events stay held in order for the next one."""
    from apps.whatsapp.models import WebhookEventLog

    _switch("whatsapp", False, operator)
    events = [
        WebhookEventLog.objects.create(
            source=WebhookEventLog.Source.WHATSAPP,
            event_type="message",
            payload={},
            held=True,
        )
        for _ in range(3)
    ]
    _switch("whatsapp", True, operator)
    with mock.patch(
        "apps.whatsapp.tasks.process_whatsapp_event.delay",
        side_effect=[None, ConnectionError("down"), None],
    ):
        replay_held_webhooks()
    for event in events[1:]:
        event.refresh_from_db()
        assert event.held is True


# --- Instagram --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_instagram_held_event_is_replayed_once(wired, operator):
    from apps.instagram.models.webhook import WebhookEventLog

    _switch("instagram", False, operator)
    event = WebhookEventLog.objects.create(
        event_id="ig:1",
        event_type="message",
        raw_payload={},
        status=WebhookEventLog.Status.HELD,
    )
    _switch("instagram", True, operator)
    with mock.patch("apps.instagram.tasks.process_instagram_event.delay") as delay:
        replay_held_webhooks()
        replay_held_webhooks()
    delay.assert_called_once_with(event.pk)
    event.refresh_from_db()
    assert event.status == WebhookEventLog.Status.RECEIVED

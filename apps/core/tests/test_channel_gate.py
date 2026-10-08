from types import SimpleNamespace

import pytest
from django.urls import reverse

from apps.conversations.actions import ActionError, _require_channel_enabled


@pytest.fixture
def email_off(settings):
    settings.EMAIL_ENABLED = False
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", ["/email/domains/", "/email/campaigns/new/", "/email/templates/create/"]
)
def test_a_disabled_channels_pages_are_404(client, email_off, path):
    assert client.get(path).status_code == 404


@pytest.mark.django_db
def test_an_enabled_channels_pages_pass_through(client, settings):
    settings.EMAIL_ENABLED = True
    assert client.get("/email/domains/").status_code == 302  # to login


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/email/campaigns/", "/email/templates/"])
def test_shared_hubs_stay_open_while_whatsapp_is_on(client, email_off, settings, path):
    assert client.get(path).status_code == 302  # to login, not 404
    settings.WHATSAPP_ENABLED = False
    assert client.get(path).status_code == 404


@pytest.mark.django_db
def test_links_in_sent_email_keep_resolving(client, email_off):
    url = reverse("email-unsubscribe", args=["not-a-real-token"])
    assert url.startswith("/email/t/")
    assert (
        client.post(
            "/email/webhooks/ses/", data="{}", content_type="application/json"
        ).status_code
        != 404
    )


@pytest.mark.django_db
def test_a_disabled_channels_webhook_acks_and_drops(client, settings):
    from apps.instagram.models.webhook import WebhookEventLog

    settings.INSTAGRAM_ENABLED = False
    resp = client.post(
        "/instagram/webhook/",
        data='{"object": "instagram"}',
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert not WebhookEventLog.objects.exists()

    settings.INSTAGRAM_VERIFY_TOKEN = "tok"
    resp = client.get(
        "/instagram/webhook/",
        {"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "c1"},
    )
    assert resp.status_code == 200 and resp.content == b"c1"


@pytest.mark.django_db
def test_meta_compliance_callbacks_stay_reachable(client, settings):
    settings.INSTAGRAM_ENABLED = False
    assert client.get("/instagram/data-deletion/").status_code != 404
    assert client.get("/instagram/accounts/").status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", ["/api/v1/messages", "/api/v1/templates/welcome", "/api/v1/campaigns"]
)
def test_a_disabled_channels_api_is_404_json(client, email_off, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


@pytest.mark.django_db
def test_other_api_resources_are_unaffected(client, email_off):
    assert client.get("/api/v1/contacts").status_code != 404


def test_replies_on_a_disabled_channel_are_refused(settings):
    settings.INSTAGRAM_ENABLED = False
    with pytest.raises(ActionError, match="Instagram is switched off"):
        _require_channel_enabled(SimpleNamespace(channel="instagram"))
    settings.INSTAGRAM_ENABLED = True
    _require_channel_enabled(SimpleNamespace(channel="instagram"))
    _require_channel_enabled(SimpleNamespace(channel="sms"))

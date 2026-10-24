"""Facebook is a registry channel (Page publishing only): switching it off holds publishing,
shows the composer and Channels page why, and closes the connect flow, but never strands a
connected Page."""

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.core import channels as ch
from apps.core.models import SiteSettings
from apps.publishing.composer import OFF, destinations
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import publish_variant
from apps.publishing.tasks import publish_variant_async

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_channel_cache():
    ch.invalidate_channel_cache()
    yield
    ch.invalidate_channel_cache()


def _switch_off(key):
    site = SiteSettings.load()
    site.disabled_channels = [key]
    site.save()


@pytest.fixture
def owner(client):
    user = User.objects.create_user("o@example.com", "o@example.com", "pw")
    account = Account.objects.create(company_name="Page Co")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return account


@pytest.fixture
def fb_variant(owner):
    post = ContentPost.objects.create(
        account=owner, platforms=["facebook"], media_type="image"
    )
    return ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        caption="hi",
        status=ContentPostVariant.Status.SCHEDULED,
    )


def test_facebook_is_a_registry_channel(settings):
    assert ch.CHANNEL_REGISTRY["facebook"].label == "Facebook"
    settings.FACEBOOK_ENABLED = True
    assert ch.is_enabled("facebook")
    settings.FACEBOOK_ENABLED = False
    assert not ch.is_enabled("facebook")


def test_switched_off_facebook_holds_the_variant_without_calling_meta(fb_variant):
    _switch_off("facebook")
    provider = MagicMock()
    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "page"),
    ):
        result = publish_variant(ContentPostVariant.objects.get(pk=fb_variant.pk))

    assert result["held"] and not result["success"]
    provider.create_photo_post.assert_not_called()
    fb_variant.refresh_from_db()
    # Untouched: still scheduled, so the scheduler sends it once Facebook is back on.
    assert fb_variant.status == ContentPostVariant.Status.SCHEDULED
    assert fb_variant.attempt_count == 0


def test_the_task_reports_a_hold_and_does_not_retry(fb_variant):
    _switch_off("facebook")
    with patch.object(publish_variant_async, "retry") as retry:
        assert publish_variant_async.run(fb_variant.pk) == "held_channel_off"
    retry.assert_not_called()


def test_instagram_switched_off_holds_too(owner):
    _switch_off("instagram")
    post = ContentPost.objects.create(
        account=owner, platforms=["instagram"], media_type="image"
    )
    variant = ContentPostVariant.objects.create(post=post, platform="instagram")
    assert publish_variant(variant)["held"]


def test_composer_shows_a_switched_off_channel(owner, client):
    assert destinations(owner)["facebook"]["status"] != OFF
    _switch_off("facebook")
    assert destinations(owner)["facebook"]["status"] == OFF
    html = client.get("/publishing/new/").content.decode()
    assert "Switched off" in html


def test_channels_page_hides_connect_while_facebook_is_off(owner, client):
    assert (
        "/publishing/fb/connect/"
        in client.get("/publishing/channels/").content.decode()
    )
    _switch_off("facebook")
    html = client.get("/publishing/channels/").content.decode()
    assert "/publishing/fb/connect/" not in html
    assert "Facebook Page publishing is switched off" in html


def test_connect_is_closed_but_disconnect_stays_open_while_off(owner, client):
    _switch_off("facebook")
    assert client.get("/publishing/fb/connect/").status_code == 404
    # Removing a connected Page always works (it's a POST that redirects back).
    assert client.post("/publishing/fb/disconnect/").status_code != 404


def test_channels_page_says_meta_review_is_pending(owner, client):
    # Facebook starts restricted (Meta App Review): say who can connect.
    html = client.get("/publishing/channels/").content.decode()
    assert "only accounts invited to test the Akilent app can connect a Page" in html
    site = SiteSettings.load()
    site.restricted_channels = []
    site.save()
    html = client.get("/publishing/channels/").content.decode()
    assert "only accounts invited to test" not in html

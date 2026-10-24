"""Onboarding sets up the channels the business picked at signup (Account.selected_channels),
never ones inferred from its plan, and never points a business at a channel it can't connect
while Meta App Review is pending."""

import pytest
from django.contrib.auth.models import User

from apps.accounts import onboarding as ob
from apps.accounts.models import Account, Membership
from apps.core import channels as ch
from apps.core.models import SiteSettings

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_channel_cache(settings):
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    settings.FACEBOOK_ENABLED = True
    settings.WHATSAPP_APP_ID = "123"
    ch.invalidate_channel_cache()
    yield
    ch.invalidate_channel_cache()


def _account(channels, services=Account.Services.BOTH):
    user = User.objects.create_user("o@example.com", "o@example.com", "pw")
    acc = Account.objects.create(
        company_name="Acme",
        selected_services=services,
        selected_channels=channels,
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=True,
    )
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


def _required(acc):
    return [s["key"] for s in ob.get_state(acc, essentials_only=True)["steps"]]


def _keys(acc):
    return [s["key"] for s in ob.get_state(acc)["steps"]]


def _open(*keys):
    site = SiteSettings.load()
    site.restricted_channels = [k for k in site.restricted_channels if k not in keys]
    site.save()


def test_only_the_picked_channels_are_set_up():
    # A "both" plan, but only WhatsApp was picked: no email domain steps.
    acc = _account(["whatsapp"])
    assert _required(acc) == ["account", "verify_email", "whatsapp"]


def test_picked_instagram_and_facebook_are_required_setup():
    acc = _account(["instagram", "facebook"])
    assert _required(acc) == [
        "account",
        "verify_email",
        "connect_instagram",
        "connect_facebook",
    ]
    assert ob.advance_onboarding(acc) == "/instagram/accounts/"
    acc.refresh_from_db()
    assert acc.onboarding_state == Account.Onboarding.INSTAGRAM_SETUP


def test_facebook_comes_after_instagram_and_completes_once_connected():
    from apps.publishing.models import FacebookPageConnection

    acc = _account(["facebook"])
    assert ob.advance_onboarding(acc) == "/publishing/channels/"
    FacebookPageConnection.objects.create(
        account=acc,
        page_id="1",
        page_name="Page",
        access_token="t",
        status=FacebookPageConnection.Status.ACTIVE,
    )
    assert ob.advance_onboarding(acc) == ""
    acc.refresh_from_db()
    assert acc.onboarding_state == Account.Onboarding.COMPLETED
    assert ob.get_state(acc)["complete"]


def test_a_switched_off_pick_is_paused_not_dropped():
    acc = _account(["facebook", "whatsapp"])
    site = SiteSettings.load()
    site.disabled_channels = ["facebook"]
    site.save()
    assert "facebook_paused" in _required(acc)
    assert "connect_facebook" not in _required(acc)


def test_an_account_from_before_channels_were_stored_uses_its_service():
    acc = _account([], services=Account.Services.EMAIL)
    assert _required(acc) == ["account", "verify_email", "domain", "verify"]


def test_restricted_instagram_is_not_recommended(monkeypatch):
    from apps.billing import api as billing_api

    monkeypatch.setattr(
        billing_api,
        "nav_state",
        lambda account: {
            "usable": frozenset({"instagram", "content_studio"}),
            "locked": frozenset(),
            "tools_off": frozenset(),
        },
    )
    acc = _account(["whatsapp"])
    assert "instagram" not in _keys(acc)
    _open("instagram")
    assert "instagram" in _keys(acc)

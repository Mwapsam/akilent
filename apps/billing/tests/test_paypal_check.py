"""manage.py paypal_check: the read-only PayPal pre-flight run before going live."""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.billing.management.commands.paypal_check import required_events
from apps.billing.paypal import PayPalError
from apps.billing.providers import registry
from apps.core.models import SiteSettings

CLIENT = "apps.billing.paypal.get_paypal_client"
WEBHOOK_URL = "https://akilent.com/billing/webhooks/paypal/"


@pytest.fixture
def live(db, settings):
    settings.PAYPAL_ENV = "live"
    settings.PAYPAL_ALLOW_SANDBOX = False
    settings.PAYPAL_CLIENT_ID = "live-client"
    settings.PAYPAL_CLIENT_SECRET = "live-secret"
    settings.PAYPAL_WEBHOOK_ID = "WH-LIVE"
    settings.PAYPAL_MERCHANT_ID = "MERCHANT"
    settings.PAYPAL_CURRENCIES = frozenset({"USD"})
    settings.BILLING_CURRENCY = "USD"
    settings.PAYPAL_SUBSCRIPTIONS = True
    settings.SITE_URL = "https://akilent.com"
    registry.reset()
    site = SiteSettings.load()
    site.billing_provider = "paypal"
    site.save()
    yield settings
    registry.reset()


def _client(events, url=WEBHOOK_URL):
    client = MagicMock()
    client.get_webhook.return_value = {
        "url": url,
        "event_types": [{"name": e} for e in events],
    }
    return client


def _run():
    out = StringIO()
    call_command("paypal_check", stdout=out)
    return out.getvalue()


def test_passes_when_everything_is_in_place(live):
    client = _client(required_events(True))
    with patch(CLIENT, return_value=client):
        out = _run()
    assert "PayPal check passed" in out
    client._token.assert_called_once_with(fresh=True)
    client.get_webhook.assert_called_once_with("WH-LIVE")
    # Secrets are never printed.
    assert "live-secret" not in out and "live-client" not in out


def test_missing_subscription_events_fail(live):
    out = StringIO()
    with patch(CLIENT, return_value=_client(required_events(False))):
        with pytest.raises(CommandError):
            call_command("paypal_check", stdout=out)
    assert "BILLING.SUBSCRIPTION.ACTIVATED" in out.getvalue()


def test_order_events_suffice_without_subscriptions(live):
    live.PAYPAL_SUBSCRIPTIONS = False
    with patch(CLIENT, return_value=_client(required_events(False))):
        out = _run()
    assert "renewals go out as pay links" in out
    assert "PayPal check passed" in out


def test_wrong_webhook_url_fails(live):
    client = _client(required_events(True), url="https://old.example.com/hook")
    with patch(CLIENT, return_value=client):
        with pytest.raises(CommandError):
            call_command("paypal_check", stdout=StringIO())


def test_refused_credentials_fail(live):
    client = MagicMock()
    client._token.side_effect = PayPalError("PayPal 401 invalid_client", status=401)
    with patch(CLIENT, return_value=client):
        with pytest.raises(CommandError):
            call_command("paypal_check", stdout=StringIO())
    client.get_webhook.assert_not_called()


def test_missing_secrets_stop_before_calling_paypal(live):
    live.PAYPAL_CLIENT_SECRET = ""
    with patch(CLIENT) as get_client:
        with pytest.raises(CommandError):
            call_command("paypal_check", stdout=StringIO())
    get_client.assert_not_called()


def test_empty_webhook_id_fails(live):
    live.PAYPAL_WEBHOOK_ID = ""
    with patch(CLIENT, return_value=_client(required_events(True))) as get_client:
        with pytest.raises(CommandError):
            call_command("paypal_check", stdout=StringIO())
    get_client.return_value.get_webhook.assert_not_called()


def test_inactive_provider_and_wildcard_webhook_only_warn(live):
    site = SiteSettings.load()
    site.billing_provider = "flutterwave"
    site.save()
    with patch(CLIENT, return_value=_client(["*"])):
        out = _run()
    assert "Active billing provider: flutterwave" in out
    assert "all events (*)" in out
    assert "PayPal check passed (2 warning(s))" in out

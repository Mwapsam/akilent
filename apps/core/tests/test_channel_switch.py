"""Admin on/off switch for channels (SiteSettings.disabled_channels) and the gate it drives."""

from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from apps.core import channels as ch
from apps.core.models import AdminAction, SiteSettings


@pytest.fixture(autouse=True)
def _fresh_channel_cache():
    # Reset every cache field, including the failure backoff, so tests can't leak state.
    reset = {"value": None, "loaded_at": 0.0, "failed_at": 0.0}
    ch._cache.update(reset)
    yield
    ch._cache.update(reset)


@pytest.fixture
def all_wired(settings):
    settings.EMAIL_ENABLED = True
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True


@pytest.fixture
def operator(db):
    return User.objects.create_superuser("ops", "ops@example.com", "pw-123456!")


@pytest.mark.django_db
def test_a_disabled_channel_is_not_enabled(all_wired):
    assert "email" in ch.enabled_channels()
    ch.set_channel_enabled("email", False, request=_fake_request())
    assert "email" not in ch.enabled_channels()
    assert "whatsapp" in ch.enabled_channels()


@pytest.mark.django_db
def test_disabled_channel_stays_disabled_after_cache_refresh(all_wired):
    ch.set_channel_enabled("email", False, request=_fake_request())
    ch.invalidate_channel_cache()
    assert "email" not in ch.enabled_channels()


@pytest.mark.django_db
def test_re_enabling_restores_the_channel(all_wired):
    ch.set_channel_enabled("email", False, request=_fake_request())
    assert ch.set_channel_enabled("email", True, request=_fake_request()) is True
    assert "email" in ch.enabled_channels()


@pytest.mark.django_db
def test_admin_cannot_enable_a_channel_the_server_does_not_have(settings):
    settings.EMAIL_ENABLED = False
    with pytest.raises(ch.ChannelUnavailableError):
        ch.set_channel_enabled("email", True, request=_fake_request())
    assert "email" not in ch.enabled_channels()


@pytest.mark.django_db
def test_stale_channel_keys_are_dropped_on_save(all_wired):
    site = SiteSettings.load()
    site.disabled_channels = ["fax", "email"]  # "fax" is no longer a registry channel
    site.save()
    site.refresh_from_db()
    assert site.disabled_channels == ["email"]


@pytest.mark.django_db
def test_database_failure_with_no_good_read_fails_closed(all_wired):
    with mock.patch.object(ch, "_read_disabled_from_db", return_value=None):
        assert ch.enabled_channels() == frozenset()
        assert ch.channel_state("email") == "unknown"
        with pytest.raises(ch.ChannelStateUnknownError):
            ch.assert_channel_enabled("email")


@pytest.mark.django_db
def test_database_failure_keeps_last_good_decision(all_wired):
    ch.set_channel_enabled("email", False, request=_fake_request())
    ch.invalidate_channel_cache()
    with mock.patch.object(ch, "_read_disabled_from_db", return_value=None):
        assert "email" not in ch.enabled_channels()


@pytest.mark.django_db
def test_switch_writes_an_audit_entry(all_wired, operator):
    client_request = _fake_request(user=operator)
    ch.set_channel_enabled("email", False, request=client_request)
    row = AdminAction.objects.get(action="channel.disabled")
    assert row.actor == operator
    assert row.target == "email"
    assert row.detail == {"channel": "email", "previous": True, "new": False}


@pytest.mark.django_db
def test_no_change_writes_no_audit_entry(all_wired, operator):
    assert (
        ch.set_channel_enabled("email", True, request=_fake_request(user=operator))
        is False
    )
    assert not AdminAction.objects.filter(action__startswith="channel.").exists()


@pytest.mark.django_db
def test_settings_page_toggles_channels(client, all_wired, operator):
    client.force_login(operator)
    url = reverse("core:settings")
    # Each channel's state as the page rendered it (channel_was_*), then the admin's choice:
    # Email was on and is now unchecked (switch off); WhatsApp was off and is now checked.
    response = client.post(
        url,
        {
            "app_name": "Automator",
            "channel_was_email": "1",
            "channel_was_whatsapp": "",
            "channel_whatsapp": "on",
        },
    )
    assert response.status_code == 302
    assert "email" not in ch.enabled_channels()
    assert "whatsapp" in ch.enabled_channels()


@pytest.mark.django_db
def test_unrelated_save_does_not_undo_another_admins_switch(
    client, all_wired, operator
):
    client.force_login(operator)
    # This admin's page was rendered with Email on. Meanwhile another admin switched it off.
    ch.set_channel_enabled("email", False, request=_fake_request(user=operator))
    client.post(
        reverse("core:settings"),
        {"app_name": "Renamed", "channel_was_email": "1", "channel_email": "on"},
    )
    assert "email" not in ch.enabled_channels()


@pytest.mark.django_db
def test_non_operator_cannot_change_channels(client, all_wired, operator):
    user = User.objects.create_user("biz", "biz@example.com", "pw-123456!")
    client.force_login(user)
    client.post(reverse("core:settings"), {"app_name": "x"})
    assert "email" in ch.enabled_channels()


@pytest.mark.django_db
def test_disabled_email_pages_404_at_runtime(client, all_wired, operator):
    ch.set_channel_enabled("email", False, request=_fake_request(user=operator))
    assert client.get("/email/domains/").status_code == 404


@pytest.mark.django_db
def test_webhook_and_tracking_paths_still_reachable_when_disabled(
    client, all_wired, operator
):
    ch.set_channel_enabled("email", False, request=_fake_request(user=operator))
    assert (
        client.post(
            "/email/webhooks/ses/", data="{}", content_type="application/json"
        ).status_code
        != 404
    )


@pytest.mark.django_db
def test_signup_offers_only_enabled_services(all_wired, operator):
    from apps.accounts.forms import enabled_service_choices
    from apps.billing.models import Plan

    Plan.objects.create(slug="e", name="E", service_type=Plan.SERVICE_EMAIL)
    Plan.objects.create(slug="b", name="B", service_type=Plan.SERVICE_BOTH)
    ch.set_channel_enabled("email", False, request=_fake_request(user=operator))
    values = [v for v, _ in enabled_service_choices()]
    # The email plan has nothing left to give; the "both" plan is a WhatsApp plan for now.
    assert values == ["whatsapp"]


@pytest.mark.django_db
def test_signup_form_rejects_a_disabled_service(all_wired, operator):
    from apps.accounts.forms import SignupForm

    ch.set_channel_enabled("email", False, request=_fake_request(user=operator))
    form = SignupForm(data={"selected_services": "email"})
    assert "selected_services" in form.errors


@pytest.mark.django_db
def test_email_send_entry_point_refuses_while_disabled(all_wired, operator):
    from apps.api.services import create_and_queue_message

    ch.set_channel_enabled("email", False, request=_fake_request(user=operator))
    with pytest.raises(ch.ChannelDisabledError):
        create_and_queue_message(
            account=None,
            from_email="a@example.com",
            to_email="b@example.com",
        )


@pytest.mark.django_db
def test_visible_choices_drop_disabled_registry_channels_only(all_wired, operator):
    ch.set_channel_enabled("email", False, request=_fake_request(user=operator))
    choices = [("email", "Email"), ("whatsapp", "WhatsApp"), ("sms", "SMS")]
    assert ch.visible_choices(choices) == [("whatsapp", "WhatsApp"), ("sms", "SMS")]


def _fake_request(user=None):
    """Audit only reads request.user; a bare object is enough outside the HTTP stack."""
    from types import SimpleNamespace

    user = user or SimpleNamespace(is_authenticated=False)
    return SimpleNamespace(user=user)


@pytest.mark.django_db
def test_conversations_on_a_disabled_channel_are_hidden_not_deleted(
    all_wired, operator
):
    from apps.accounts.models import Account
    from apps.contacts.models import Contact
    from apps.conversations.models import Conversation

    account = Account.objects.create(company_name="Acme")
    contact = Contact.objects.create(account=account, phone="+260971000100")
    Conversation.objects.create(account=account, contact=contact, channel="email")
    Conversation.objects.create(account=account, contact=contact, channel="whatsapp")
    qs = Conversation.objects.filter(account=account)

    ch.set_channel_enabled("email", False, request=_fake_request(user=operator))
    assert {c.channel for c in ch.hide_disabled_channels(qs)} == {"whatsapp"}
    assert Conversation.objects.filter(account=account, channel="email").exists()

    ch.set_channel_enabled("email", True, request=_fake_request(user=operator))
    assert {c.channel for c in ch.hide_disabled_channels(qs)} == {"email", "whatsapp"}


@pytest.mark.django_db
def test_system_email_still_sends_with_the_email_channel_off(all_wired, operator):
    from apps.email.services import send as send_mod

    ch.set_channel_enabled("email", False, request=_fake_request(user=operator))
    provider = mock.Mock()
    with (
        mock.patch(
            "apps.email.providers.get_system_email_provider", return_value=provider
        ),
        mock.patch(
            "apps.email.services.suppression.is_suppressed_globally", return_value=False
        ),
        mock.patch(
            "apps.email.services.validation.validate_recipient", return_value=True
        ),
    ):
        send_mod.send_system_email("user@example.com", "Reset your password", "body")
    provider.send.assert_called_once()


@pytest.mark.django_db
def test_system_email_provider_ignores_tenant_send_backend(settings, all_wired):
    from apps.core.models import MailProviderSettings
    from apps.email.providers import get_system_email_provider
    from apps.email.providers.null import NullSendProvider

    settings.SYSTEM_EMAIL_PROVIDER_BACKEND = "null"
    mps = MailProviderSettings.load()
    mps.send_backend = "ses"  # tenant send provider; must not affect system mail
    mps.save()
    assert isinstance(get_system_email_provider(), NullSendProvider)

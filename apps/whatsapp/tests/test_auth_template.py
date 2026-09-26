"""Authentication (one-time code) templates: Meta writes the wording, Akilent sends only the options."""
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import override_settings

from apps.accounts.models import Account, Membership
from apps.whatsapp.models import MessageTemplate
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.template_builder import TemplateBuilderError, build_auth_payload, validate_auth_options

_wa_urls = override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled", WHATSAPP_ENABLED=True)


def test_the_payload_is_metas_authentication_shape():
    payload = build_auth_payload(name="login_code", language="en", security_recommendation=True,
                                 expiry_minutes=10, button_text="")
    assert payload == {
        "name": "login_code", "language": "en", "category": "AUTHENTICATION",
        "components": [
            {"type": "BODY", "add_security_recommendation": True},
            {"type": "FOOTER", "code_expiration_minutes": 10},
            {"type": "BUTTONS", "buttons": [{"type": "OTP", "otp_type": "COPY_CODE", "text": "Copy code"}]},
        ],
    }
    no_expiry = build_auth_payload(name="x", language="en", security_recommendation=False, expiry_minutes=None)
    assert [c["type"] for c in no_expiry["components"]] == ["BODY", "BUTTONS"]
    assert "text" not in no_expiry["components"][0], "Meta rejects custom body text on authentication templates"


@pytest.mark.parametrize("expiry", ["0", "91", "ten"])
def test_expiry_must_be_1_to_90_minutes(expiry):
    with pytest.raises(TemplateBuilderError):
        validate_auth_options(name="login_code", language="en", expiry_minutes=expiry, button_text="")


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    WhatsAppBusinessNumber.objects.create(account=account, phone_number_id="PNID", waba_id="WABA1",
                                          access_token="tok", is_active=True)
    client.force_login(user)
    return client, account


@_wa_urls
@pytest.mark.django_db
def test_submitting_an_authentication_template_sends_only_the_options(logged_in):
    client, account = logged_in
    with patch("apps.whatsapp.template_builder.get_whatsapp_provider") as get_provider:
        get_provider.return_value.create_template.return_value = {"id": "1", "status": "PENDING"}
        resp = client.post("/whatsapp/templates/new/", {
            "name": "login_code", "category": "authentication", "language": "en",
            "body": "", "auth_security": "on", "auth_expiry": "5", "auth_button_text": "Copy",
        })
        sent = get_provider.return_value.create_template.call_args.args[1]
    assert resp.status_code == 302
    assert sent["category"] == "AUTHENTICATION" and sent["components"][1] == {"type": "FOOTER", "code_expiration_minutes": 5}
    tpl = MessageTemplate.objects.get(account=account, whatsapp_template_name="login_code")
    assert tpl.approval_status == MessageTemplate.ApprovalStatus.PENDING and tpl.category == "authentication"
    assert tpl.content.startswith("*{{1}}* is your verification code.") and tpl.footer == "This code expires in 5 minutes."


@_wa_urls
@pytest.mark.django_db
def test_the_form_has_the_one_time_code_options(logged_in):
    client, _ = logged_in
    html = client.get("/whatsapp/templates/new/").content.decode()
    assert 'id="wa-auth-options"' in html and 'name="auth_expiry"' in html

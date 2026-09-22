from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import override_settings

from apps.accounts.models import Account, Membership
from apps.whatsapp.models import MessageTemplate
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

_wa_urls = override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled", WHATSAPP_ENABLED=True)


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    WhatsAppBusinessNumber.objects.create(
        account=account, phone_number_id="PNID", waba_id="WABA1",
        access_token="tok", is_active=True,
    )
    client.force_login(user)
    return client, account


@_wa_urls
@pytest.mark.django_db
def test_create_template_submits_and_redirects(logged_in):
    client, account = logged_in
    with patch("apps.whatsapp.template_builder.get_whatsapp_provider") as get_provider:
        get_provider.return_value.create_template.return_value = {"id": "1", "status": "PENDING"}
        resp = client.post("/whatsapp/templates/new/", {
            "name": "payment_reminder", "category": "utility", "language": "en",
            "body": "Hi {{1}}, order {{2}} is unpaid.", "header": "", "footer": "",
            "variable_label": ["Customer name", "Order number"],
            "variable_example": ["Ada", "1029"],
        })
    assert resp.status_code == 302
    assert resp["Location"] == "/email/templates/?channel=whatsapp"
    tpl = MessageTemplate.objects.get(account=account, whatsapp_template_name="payment_reminder")
    assert tpl.approval_status == MessageTemplate.ApprovalStatus.PENDING


@_wa_urls
@pytest.mark.django_db
def test_create_template_invalid_name_reshows_form(logged_in):
    client, _ = logged_in
    resp = client.post("/whatsapp/templates/new/", {
        "name": "Payment Reminder", "category": "utility", "language": "en",
        "body": "Hi {{1}}", "variable_label": ["name"], "variable_example": ["Ada"],
    })
    assert resp.status_code == 200
    assert MessageTemplate.objects.count() == 0


@_wa_urls
@pytest.mark.django_db
def test_create_template_page_renders(logged_in):
    client, _ = logged_in
    resp = client.get("/whatsapp/templates/new/")
    assert resp.status_code == 200
    assert "New WhatsApp template" in resp.content.decode()


@_wa_urls
@pytest.mark.django_db
def test_create_template_page_includes_starter_library(logged_in):
    client, _ = logged_in
    resp = client.get("/whatsapp/templates/new/")
    body = resp.content.decode()
    assert "Start with a template" in body
    assert "payment_reminder" in body  # a starter slug present in the JSON blob


@_wa_urls
@pytest.mark.django_db
def test_create_template_from_starter_slug(logged_in):
    client, account = logged_in
    with patch("apps.whatsapp.template_builder.get_whatsapp_provider") as get_provider:
        get_provider.return_value.create_template.return_value = {"id": "1", "status": "PENDING"}
        resp = client.post("/whatsapp/templates/new/", {
            "name": "payment_reminder", "category": "utility", "language": "en",
            "body": "Hi {{1}}, your order {{2}} is still awaiting payment.\n\nComplete your payment here:\n{{3}}",
            "variable_label": ["Customer name", "Order number", "Payment link"],
            "variable_example": ["Ada", "1029", "https://pay.example.com/1029"],
        })
    assert resp.status_code == 302
    tpl = MessageTemplate.objects.get(account=account, whatsapp_template_name="payment_reminder")
    assert tpl.variables == ["Customer name", "Order number", "Payment link"]


@_wa_urls
@pytest.mark.django_db
def test_templates_page_has_create_button(logged_in):
    client, _ = logged_in
    resp = client.get("/email/templates/?channel=whatsapp")
    assert resp.status_code == 200
    assert "/whatsapp/templates/new/" in resp.content.decode()

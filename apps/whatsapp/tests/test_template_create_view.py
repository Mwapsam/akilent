from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from apps.accounts.models import Account, Membership
from apps.whatsapp.models import MessageTemplate, MessageTemplateAsset
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.types import MediaHandleResult

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
    assert "Set up your template" in body
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


@_wa_urls
@pytest.mark.django_db
def test_media_upload_returns_handle(logged_in):
    client, account = logged_in
    with patch("apps.whatsapp.views.get_whatsapp_provider") as get_provider:
        get_provider.return_value.upload_template_media.return_value = MediaHandleResult(handle="handle-abc")
        resp = client.post("/whatsapp/templates/media/upload/", {
            "header_format": "image",
            "file": SimpleUploadedFile("logo.png", b"fake-bytes", content_type="image/png"),
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["handle"] == "handle-abc"
    asset = MessageTemplateAsset.objects.get(pk=data["asset_id"])
    assert asset.account_id == account.id
    assert asset.meta_handle == "handle-abc"


@_wa_urls
@pytest.mark.django_db
def test_media_upload_rejects_unsupported_content_type(logged_in):
    client, _ = logged_in
    resp = client.post("/whatsapp/templates/media/upload/", {
        "header_format": "image",
        "file": SimpleUploadedFile("doc.txt", b"hello", content_type="text/plain"),
    })
    assert resp.status_code == 400
    assert MessageTemplateAsset.objects.count() == 0


@_wa_urls
@pytest.mark.django_db
def test_media_upload_rejects_oversized_file(logged_in):
    client, _ = logged_in
    resp = client.post("/whatsapp/templates/media/upload/", {
        "header_format": "image",
        "file": SimpleUploadedFile("logo.png", b"x" * (6 * 1024 * 1024), content_type="image/png"),
    })
    assert resp.status_code == 400
    assert MessageTemplateAsset.objects.count() == 0


@_wa_urls
@pytest.mark.django_db
def test_media_upload_deletes_asset_if_meta_upload_fails(logged_in):
    from apps.whatsapp.providers import WhatsAppProviderError

    client, _ = logged_in
    with patch("apps.whatsapp.views.get_whatsapp_provider") as get_provider:
        get_provider.return_value.upload_template_media.side_effect = WhatsAppProviderError("nope")
        resp = client.post("/whatsapp/templates/media/upload/", {
            "header_format": "image",
            "file": SimpleUploadedFile("logo.png", b"fake-bytes", content_type="image/png"),
        })
    assert resp.status_code == 400
    assert MessageTemplateAsset.objects.count() == 0


@_wa_urls
@pytest.mark.django_db
def test_create_template_with_media_header_and_phone_button(logged_in):
    client, account = logged_in
    asset = MessageTemplateAsset.objects.create(
        account=account,
        file=SimpleUploadedFile("logo.png", b"fake-bytes", content_type="image/png"),
        content_type="image/png", meta_handle="handle-abc",
    )
    with patch("apps.whatsapp.template_builder.get_whatsapp_provider") as get_provider:
        get_provider.return_value.create_template.return_value = {"id": "1", "status": "PENDING"}
        resp = client.post("/whatsapp/templates/new/", {
            "name": "promo", "category": "marketing", "language": "en",
            "body": "Big sale this week!",
            "header_format": "image", "header_media_asset_id": asset.id,
            "button_type": "phone_number", "button_text": "Call us",
            "button_phone_number": "+15551234567",
        })
    assert resp.status_code == 302
    tpl = MessageTemplate.objects.get(account=account, whatsapp_template_name="promo")
    assert tpl.header_format == "image"
    assert tpl.header_media_id == asset.id
    assert tpl.buttons == [{"type": "PHONE_NUMBER", "text": "Call us", "phone_number": "+15551234567"}]

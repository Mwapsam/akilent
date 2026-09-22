"""WhatsApp templates are managed on the WhatsApp tab of /email/templates/ —
one channel-neutral "Templates" page, same pattern as Campaigns (R1.5c
follow-up). Meta is the source of truth; the only write path here is
"Sync from WhatsApp"."""
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
    client.force_login(user)
    return client, account


@_wa_urls
@pytest.mark.django_db
def test_whatsapp_tab_shows_synced_templates_not_shown_on_email_tab(logged_in):
    client, account = logged_in
    MessageTemplate.objects.create(
        account=account, name="Order update", whatsapp_template_name="order_update",
        language_code="en", approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )

    resp = client.get("/email/templates/?channel=whatsapp")
    assert resp.status_code == 200
    assert "Order update" in resp.content.decode()

    resp = client.get("/email/templates/")
    assert "Order update" not in resp.content.decode()


@_wa_urls
@pytest.mark.django_db
def test_whatsapp_templates_scoped_to_account(logged_in):
    client, account = logged_in
    other = Account.objects.create(company_name="Other Co")
    MessageTemplate.objects.create(
        account=other, name="Other's template", whatsapp_template_name="other",
        language_code="en",
    )
    resp = client.get("/email/templates/?channel=whatsapp")
    assert "Other's template" not in resp.content.decode()


@_wa_urls
@pytest.mark.django_db
def test_sync_button_pulls_from_meta_and_redirects_to_whatsapp_tab(logged_in):
    client, account = logged_in
    WhatsAppBusinessNumber.objects.create(
        account=account, phone_number_id="PNID", waba_id="WABA1",
        access_token="tok", is_active=True,
    )

    class _Provider:
        def list_templates(self, waba_id):
            return [{"name": "promo", "language": "en", "category": "MARKETING", "status": "APPROVED"}]

    with patch("apps.whatsapp.providers.get_whatsapp_provider", return_value=_Provider()):
        resp = client.post("/whatsapp/templates/sync/")

    assert resp.status_code == 302
    assert resp["Location"] == "/email/templates/?channel=whatsapp"
    assert MessageTemplate.objects.filter(account=account, whatsapp_template_name="promo").exists()


@_wa_urls
@pytest.mark.django_db
def test_sync_is_post_only(logged_in):
    client, _ = logged_in
    resp = client.get("/whatsapp/templates/sync/")
    assert resp.status_code == 405

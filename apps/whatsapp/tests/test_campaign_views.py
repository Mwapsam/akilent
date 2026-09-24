import pytest
from django.contrib.auth.models import User
from django.test import override_settings

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact, ContactList
from apps.whatsapp.models import MessageTemplate, WhatsAppCampaign, WhatsAppContact

# The project only mounts /whatsapp/ when WHATSAPP_ENABLED (off by default in
# tests) — same fixture other apps.whatsapp view tests use (test_registration.py).
_wa_urls = override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled")


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


@pytest.fixture
def approved_template(logged_in):
    _, account = logged_in
    return MessageTemplate.objects.create(
        account=account, name="Promo", whatsapp_template_name="promo", content="Hi!",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )


@pytest.fixture
def contact_list(logged_in):
    _, account = logged_in
    contact_list = ContactList.objects.create(account=account, name="VIPs")
    contact = Contact.objects.create(account=account, phone="+260971111111")
    WhatsAppContact.objects.create(
        account=account, phone_number=contact.phone, contact=contact,
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_IN,
    )
    contact_list.contacts.add(contact)
    return contact_list


@_wa_urls
@pytest.mark.django_db
def test_campaign_new_page_renders(logged_in, approved_template, contact_list):
    client, _ = logged_in
    resp = client.get("/whatsapp/campaigns/new/")
    assert resp.status_code == 200
    assert "VIPs" in resp.content.decode()
    assert "Promo" in resp.content.decode()


@_wa_urls
@pytest.mark.django_db
def test_create_campaign_via_view_redirects_to_detail(
    logged_in, approved_template, contact_list, django_capture_on_commit_callbacks,
):
    client, account = logged_in
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post("/whatsapp/campaigns/new/", {
            "name": "Launch", "contact_list": contact_list.pk, "template_id": approved_template.pk,
        })
    assert resp.status_code == 302
    campaign = WhatsAppCampaign.objects.get(account=account, name="Launch")
    assert resp["Location"] == f"/whatsapp/campaigns/{campaign.pk}/"
    assert campaign.status == WhatsAppCampaign.Status.COMPLETED


@_wa_urls
@pytest.mark.django_db
def test_create_campaign_cannot_use_another_accounts_contact_list(logged_in, approved_template):
    client, account = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_list = ContactList.objects.create(account=other, name="Other list")

    resp = client.post("/whatsapp/campaigns/new/", {
        "name": "X", "contact_list": other_list.pk, "template_id": approved_template.pk,
    }, follow=True)
    assert resp.status_code == 200
    assert b"Choose a customer list" in resp.content
    assert not WhatsAppCampaign.objects.filter(account=account).exists()


@_wa_urls
@pytest.mark.django_db
def test_campaign_detail_scoped_to_account(logged_in):
    client, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_list = ContactList.objects.create(account=other, name="Other list")
    other_template = MessageTemplate.objects.create(
        account=other, name="T", whatsapp_template_name="t", content="x",
    )
    other_campaign = WhatsAppCampaign.objects.create(
        account=other, name="Other", contact_list=other_list, template=other_template,
    )
    resp = client.get(f"/whatsapp/campaigns/{other_campaign.pk}/")
    assert resp.status_code == 404


@_wa_urls
@override_settings(WHATSAPP_ENABLED=True)
@pytest.mark.django_db
def test_campaigns_list_shows_whatsapp_tab(logged_in, django_capture_on_commit_callbacks):
    client, account = logged_in
    contact_list = ContactList.objects.create(account=account, name="Everyone")
    template = MessageTemplate.objects.create(
        account=account, name="T", whatsapp_template_name="t", content="x",
    )
    WhatsAppCampaign.objects.create(
        account=account, name="Sept promo", contact_list=contact_list, template=template,
        status=WhatsAppCampaign.Status.COMPLETED,
    )
    resp = client.get("/email/campaigns/?channel=whatsapp")
    assert resp.status_code == 200
    assert "Sept promo" in resp.content.decode()

    # Email tab (default) never shows the WhatsApp row.
    resp = client.get("/email/campaigns/")
    assert "Sept promo" not in resp.content.decode()


# --- Dead-end guards ------------------------------------------------------
# Both selects are `required`, so a form rendered with no options could never be
# satisfied and nothing would say why. The page already sidesteps that by
# replacing the form with an explanation; these pin that it keeps doing so.

@_wa_urls
@pytest.mark.django_db
def test_campaign_new_explains_a_missing_contact_list(logged_in, approved_template):
    client, _ = logged_in
    body = client.get("/whatsapp/campaigns/new/").content.decode()
    assert "have any customer lists yet" in body
    assert 'href="/contacts/"' in body
    # The unsatisfiable form must not be shown at all.
    assert 'name="contact_list"' not in body


@_wa_urls
@pytest.mark.django_db
def test_campaign_new_explains_a_missing_template(logged_in, contact_list):
    client, _ = logged_in
    body = client.get("/whatsapp/campaigns/new/").content.decode()
    assert "Meta-approved WhatsApp templates yet" in body
    assert 'name="template_id"' not in body


@_wa_urls
@pytest.mark.django_db
def test_campaign_new_shows_the_form_when_both_exist(logged_in, approved_template, contact_list):
    client, _ = logged_in
    body = client.get("/whatsapp/campaigns/new/").content.decode()
    assert 'name="contact_list"' in body
    assert 'name="template_id"' in body
    assert "have any customer lists yet" not in body

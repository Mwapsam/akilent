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
        account=account,
        name="Promo",
        whatsapp_template_name="promo",
        content="Hi!",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )


@pytest.fixture
def contact_list(logged_in):
    _, account = logged_in
    contact_list = ContactList.objects.create(account=account, name="VIPs")
    contact = Contact.objects.create(account=account, phone="+260971111111")
    WhatsAppContact.objects.create(
        account=account,
        phone_number=contact.phone,
        contact=contact,
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
    logged_in,
    approved_template,
    contact_list,
    django_capture_on_commit_callbacks,
):
    client, account = logged_in
    with django_capture_on_commit_callbacks(execute=True):
        resp = client.post(
            "/whatsapp/campaigns/new/",
            {
                "name": "Launch",
                "contact_list": contact_list.pk,
                "template_id": approved_template.pk,
            },
        )
    assert resp.status_code == 302
    campaign = WhatsAppCampaign.objects.get(account=account, name="Launch")
    assert resp["Location"] == f"/whatsapp/campaigns/{campaign.pk}/"
    assert campaign.status == WhatsAppCampaign.Status.COMPLETED


@_wa_urls
@pytest.mark.django_db
def test_create_campaign_cannot_use_another_accounts_contact_list(
    logged_in, approved_template
):
    client, account = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_list = ContactList.objects.create(account=other, name="Other list")

    resp = client.post(
        "/whatsapp/campaigns/new/",
        {
            "name": "X",
            "contact_list": other_list.pk,
            "template_id": approved_template.pk,
        },
    )
    # Re-shown with the reason (and the owner's choices kept), not a redirect.
    assert resp.status_code == 400
    assert b"Choose who to send to." in resp.content
    assert not WhatsAppCampaign.objects.filter(account=account).exists()


@_wa_urls
@pytest.mark.django_db
def test_campaign_detail_scoped_to_account(logged_in):
    client, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_list = ContactList.objects.create(account=other, name="Other list")
    other_template = MessageTemplate.objects.create(
        account=other,
        name="T",
        whatsapp_template_name="t",
        content="x",
    )
    other_campaign = WhatsAppCampaign.objects.create(
        account=other,
        name="Other",
        contact_list=other_list,
        template=other_template,
    )
    resp = client.get(f"/whatsapp/campaigns/{other_campaign.pk}/")
    assert resp.status_code == 404


@_wa_urls
@override_settings(WHATSAPP_ENABLED=True)
@pytest.mark.django_db
def test_campaigns_list_shows_whatsapp_tab(
    logged_in, django_capture_on_commit_callbacks
):
    client, account = logged_in
    contact_list = ContactList.objects.create(account=account, name="Everyone")
    template = MessageTemplate.objects.create(
        account=account,
        name="T",
        whatsapp_template_name="t",
        content="x",
    )
    WhatsAppCampaign.objects.create(
        account=account,
        name="Sept promo",
        contact_list=contact_list,
        template=template,
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
    assert "Make a list of people to send to" in body
    assert 'href="/contacts/lists/"' in body
    # The unsatisfiable form must not be shown at all.
    assert 'name="contact_list"' not in body


@_wa_urls
@pytest.mark.django_db
def test_campaign_new_explains_a_missing_template(logged_in, contact_list):
    client, _ = logged_in
    body = client.get("/whatsapp/campaigns/new/").content.decode()
    assert "Get a message template approved by Meta" in body
    assert 'name="template_id"' not in body


@_wa_urls
@pytest.mark.django_db
def test_campaign_new_shows_the_form_when_both_exist(
    logged_in, approved_template, contact_list
):
    client, _ = logged_in
    body = client.get("/whatsapp/campaigns/new/").content.decode()
    assert 'name="audience"' in body
    assert 'name="template_id"' in body
    assert "Make a list of people to send to" not in body


# --- Audit 2026-10-19: the stepped composer and the results page -------------


@_wa_urls
@pytest.mark.django_db
def test_estimate_shows_who_a_promotion_reaches_and_why_not(
    logged_in, approved_template
):
    client, account = logged_in
    approved_template.category = MessageTemplate.Category.MARKETING
    approved_template.content = "Hi {{1}}, 20% off today!"
    approved_template.variables = ["name"]
    approved_template.save()
    clist = ContactList.objects.create(account=account, name="VIPs")
    opted = Contact.objects.create(
        account=account, phone="+260971111111", first_name="Ada"
    )
    WhatsAppContact.objects.create(
        account=account,
        phone_number=opted.phone,
        contact=opted,
        opt_in_status=WhatsAppContact.OptInStatus.OPTED_IN,
    )
    unknown = Contact.objects.create(account=account, phone="+260971111112")
    nophone = Contact.objects.create(account=account, email="x@example.com")
    clist.contacts.add(opted, unknown, nophone)

    resp = client.get(
        "/whatsapp/campaigns/estimate/",
        {
            "contact_list": clist.pk,
            "template_id": approved_template.pk,
            f"var__{approved_template.pk}__name": "first_name",
        },
    )

    body = resp.content.decode()
    assert resp.status_code == 200
    assert "Hi Ada, 20% off today!" in body  # preview with a real customer
    assert "Haven&#x27;t opted in" in body and "No WhatsApp number" in body
    assert "Send to 1 customer" in body


@_wa_urls
@pytest.mark.django_db
def test_estimate_ignores_another_accounts_list(logged_in, approved_template):
    client, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    other_list = ContactList.objects.create(account=other, name="Theirs")
    resp = client.get(
        "/whatsapp/campaigns/estimate/",
        {"contact_list": other_list.pk, "template_id": approved_template.pk},
    )
    assert "Theirs" not in resp.content.decode()


@_wa_urls
@pytest.mark.django_db
def test_double_submitting_the_composer_creates_one_campaign(
    logged_in, approved_template, contact_list
):
    client, account = logged_in
    data = {
        "contact_list": contact_list.pk,
        "template_id": approved_template.pk,
        "submission_key": "page-load-1",
    }
    first = client.post("/whatsapp/campaigns/new/", data)
    second = client.post("/whatsapp/campaigns/new/", data)
    assert first["Location"] == second["Location"]
    assert WhatsAppCampaign.objects.filter(account=account).count() == 1


@_wa_urls
@pytest.mark.django_db
def test_composer_opens_on_the_message_step_when_linked_from_a_list(
    logged_in, approved_template, contact_list
):
    client, _ = logged_in
    body = client.get(
        f"/whatsapp/campaigns/new/?list={contact_list.pk}"
    ).content.decode()
    assert "step: 2" in body
    assert f"audience: 'list:{contact_list.pk}'" in body


@_wa_urls
@pytest.mark.django_db
def test_media_header_templates_are_shown_but_not_choosable(logged_in, contact_list):
    client, account = logged_in
    MessageTemplate.objects.create(
        account=account,
        name="Picture promo",
        whatsapp_template_name="pic",
        content="x",
        header_format=MessageTemplate.HeaderFormat.IMAGE,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    body = client.get("/whatsapp/campaigns/new/").content.decode()
    assert "Picture promo" in body
    assert "campaigns can't send those yet" in body


@_wa_urls
@pytest.mark.django_db
def test_detail_explains_who_was_not_sent_to_and_polls_while_sending(
    logged_in, approved_template, contact_list
):
    from apps.whatsapp.models import WhatsAppCampaignRecipient

    client, account = logged_in
    campaign = WhatsAppCampaign.objects.create(
        account=account,
        name="Promo",
        contact_list=contact_list,
        template=approved_template,
        status=WhatsAppCampaign.Status.SENDING,
        recipient_count=1,
        skipped_count=1,
    )
    WhatsAppCampaignRecipient.objects.create(
        campaign=campaign,
        contact=contact_list.contacts.first(),
        status=WhatsAppCampaignRecipient.Status.SKIPPED,
        skip_reason=WhatsAppCampaignRecipient.SkipReason.NOT_OPTED_IN,
    )

    body = client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode()
    assert "Not sent to 1 customer" in body
    assert "Haven&#x27;t opted in" in body
    assert 'hx-trigger="every 5s"' in body

    live = client.get(
        f"/whatsapp/campaigns/{campaign.pk}/?part=live", HTTP_HX_REQUEST="true"
    ).content.decode()
    assert (
        live.lstrip().startswith('<div id="campaign-live"')
        or "campaign-live" in live[:200]
    )


@_wa_urls
@pytest.mark.django_db
def test_failed_campaign_says_why(logged_in, approved_template, contact_list):
    client, account = logged_in
    campaign = WhatsAppCampaign.objects.create(
        account=account,
        name="Promo",
        contact_list=contact_list,
        template=approved_template,
        status=WhatsAppCampaign.Status.FAILED,
        error="Template 'promo' can't be sent yet.",
    )
    body = client.get(f"/whatsapp/campaigns/{campaign.pk}/").content.decode()
    assert "This campaign stopped" in body
    assert "can&#x27;t be sent yet" in body
    assert 'hx-trigger="every 5s"' not in body


@_wa_urls
@override_settings(WHATSAPP_ENABLED=True)
@pytest.mark.django_db
def test_campaigns_home_shows_readiness_checklist_first(logged_in):
    client, _ = logged_in
    body = client.get("/email/campaigns/?channel=whatsapp").content.decode()
    assert "Send your first WhatsApp campaign" in body
    assert "Connect your WhatsApp number" in body

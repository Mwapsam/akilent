"""The screens that build a campaign audience: Customers bulk-add, paste / CSV
import, list rename/delete, "everyone who messaged us", and profile consent."""

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact, ContactList
from apps.whatsapp.models import WhatsAppContact

_Opt = WhatsAppContact.OptInStatus


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.mark.django_db
def test_paste_numbers_into_a_list_shows_the_result(logged_in):
    client, account, _ = logged_in
    clist = ContactList.objects.create(account=account, name="VIPs")

    resp = client.post(
        "/contacts/import/",
        {
            "mode": "paste",
            "list": clist.pk,
            "numbers": "0977000111, Ada\nnope",
            "attested": "1",
        },
    )

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "1 added to VIPs" in body
    assert "need" in body and "nope" in body
    assert clist.contacts.count() == 1
    assert WhatsAppContact.objects.get(account=account).opt_in_status == _Opt.OPTED_IN


@pytest.mark.django_db
def test_rejected_rows_download_is_scoped_to_the_account(logged_in):
    client, account, _ = logged_in
    client.post("/contacts/import/", {"mode": "paste", "numbers": "nope"})
    from apps.contacts.models import ContactImport

    imp = ContactImport.objects.get(account=account)
    resp = client.get(f"/contacts/import/{imp.pk}/rejected.csv")
    assert resp.status_code == 200
    assert "nope" in resp.content.decode()

    other = Account.objects.create(company_name="Other")
    theirs = ContactImport.objects.create(account=other)
    assert client.get(f"/contacts/import/{theirs.pk}/rejected.csv").status_code == 404


@pytest.mark.django_db
def test_csv_upload_into_a_new_list(logged_in):
    client, account, _ = logged_in
    upload = SimpleUploadedFile(
        "customers.csv", b"Name,Phone\nAda Mwape,0977000111\n", content_type="text/csv"
    )
    resp = client.post(
        "/contacts/import/",
        {"mode": "csv", "list": "new", "new_name": "Market day", "file": upload},
    )
    assert resp.status_code == 200
    clist = ContactList.objects.get(account=account, name="Market day")
    assert [c.first_name for c in clist.contacts.all()] == ["Ada"]


@pytest.mark.django_db
def test_csv_without_phone_column_explains(logged_in):
    client, account, _ = logged_in
    upload = SimpleUploadedFile("c.csv", b"email\na@b.co\n", content_type="text/csv")
    resp = client.post(
        "/contacts/import/", {"mode": "csv", "file": upload}, follow=True
    )
    assert "phone number column" in resp.content.decode()
    assert not Contact.objects.filter(account=account).exists()


@pytest.mark.django_db
def test_bulk_add_selected_customers(logged_in):
    client, account, _ = logged_in
    a = Contact.objects.create(account=account, phone="+260977000111")
    Contact.objects.create(account=account, phone="+260977000222")
    clist = ContactList.objects.create(account=account, name="VIPs")

    resp = client.post(
        "/contacts/bulk/add-to-list/", {"ids": [a.public_id], "list": clist.pk}
    )

    assert resp["Location"] == f"/contacts/lists/{clist.pk}/"
    assert list(clist.contacts.all()) == [a]


@pytest.mark.django_db
def test_bulk_add_all_matching_reapplies_the_filter_into_a_new_list(logged_in):
    client, account, _ = logged_in
    Contact.objects.create(account=account, phone="+260977000111", first_name="Ada")
    Contact.objects.create(account=account, phone="+260977000222", first_name="Ada")
    Contact.objects.create(account=account, phone="+260977000333", first_name="Bwalya")
    other = Account.objects.create(company_name="Other")
    Contact.objects.create(account=other, phone="+260977000444", first_name="Ada")

    client.post(
        "/contacts/bulk/add-to-list/",
        {"all_matching": "1", "filter": "q=Ada", "list": "new", "new_name": "Adas"},
    )

    clist = ContactList.objects.get(account=account, name="Adas")
    assert clist.contacts.count() == 2


@pytest.mark.django_db
def test_customers_page_filters_by_whatsapp_status(logged_in):
    client, account, _ = logged_in
    opted = Contact.objects.create(
        account=account, phone="+260977000111", first_name="Opted"
    )
    WhatsAppContact.objects.create(
        account=account,
        phone_number=opted.phone,
        contact=opted,
        opt_in_status=_Opt.OPTED_IN,
    )
    Contact.objects.create(
        account=account, email="mail@example.com", first_name="Mailonly"
    )

    body = client.get("/contacts/?wa=opted_in").content.decode()
    assert "Opted" in body and "Mailonly" not in body
    body = client.get("/contacts/?wa=none").content.decode()
    assert "Mailonly" in body


@pytest.mark.django_db
def test_list_page_shows_readiness_and_send_link(logged_in, settings):
    client, account, _ = logged_in
    clist = ContactList.objects.create(account=account, name="VIPs")
    opted = Contact.objects.create(account=account, phone="+260977000111")
    WhatsAppContact.objects.create(
        account=account,
        phone_number=opted.phone,
        contact=opted,
        opt_in_status=_Opt.OPTED_IN,
    )
    clist.contacts.add(
        opted, Contact.objects.create(account=account, email="x@example.com")
    )

    body = client.get(f"/contacts/lists/{clist.pk}/").content.decode()

    assert "Who a WhatsApp campaign can reach" in body
    assert "1 opted in, 0 updates only, 0 opted out, 1 no WhatsApp" in body


@pytest.mark.django_db
def test_add_everyone_who_messaged_skips_opted_out(logged_in):
    from django.utils import timezone

    client, account, _ = logged_in
    clist = ContactList.objects.create(account=account, name="Chatters")
    talked = Contact.objects.create(account=account, phone="+260977000111")
    stopped = Contact.objects.create(account=account, phone="+260977000222")
    WhatsAppContact.objects.filter(contact__in=[talked, stopped]).delete()
    WhatsAppContact.objects.create(
        account=account,
        phone_number=talked.phone,
        contact=talked,
        last_message_at=timezone.now(),
    )
    WhatsAppContact.objects.create(
        account=account,
        phone_number=stopped.phone,
        contact=stopped,
        last_message_at=timezone.now(),
        opt_in_status=_Opt.OPTED_OUT,
    )
    Contact.objects.create(account=account, phone="+260977000333")  # never messaged

    client.post(f"/contacts/lists/{clist.pk}/add-messaged/")

    assert list(clist.contacts.all()) == [talked]


@pytest.mark.django_db
def test_rename_and_delete_list(logged_in):
    client, account, _ = logged_in
    clist = ContactList.objects.create(account=account, name="VIPs")
    ContactList.objects.create(account=account, name="Taken")

    client.post(f"/contacts/lists/{clist.pk}/rename/", {"name": "Taken"})
    clist.refresh_from_db()
    assert clist.name == "VIPs"
    client.post(f"/contacts/lists/{clist.pk}/rename/", {"name": "Best customers"})
    clist.refresh_from_db()
    assert (clist.name, clist.slug) == ("Best customers", "best-customers")

    member = Contact.objects.create(account=account, phone="+260977000111")
    clist.contacts.add(member)
    resp = client.post(f"/contacts/lists/{clist.pk}/delete/")
    assert resp["Location"] == "/contacts/lists/"
    assert not ContactList.objects.filter(pk=clist.pk).exists()
    assert Contact.objects.filter(pk=member.pk).exists()


@pytest.mark.django_db
def test_a_list_used_by_a_campaign_is_kept(logged_in):
    from apps.whatsapp.models import MessageTemplate, WhatsAppCampaign

    client, account, _ = logged_in
    clist = ContactList.objects.create(account=account, name="VIPs")
    template = MessageTemplate.objects.create(
        account=account, name="T", whatsapp_template_name="t", content="Hi"
    )
    WhatsAppCampaign.objects.create(
        account=account, name="C", contact_list=clist, template=template
    )
    client.post(f"/contacts/lists/{clist.pk}/delete/")
    assert ContactList.objects.filter(pk=clist.pk).exists()


@pytest.mark.django_db
def test_another_accounts_list_is_not_reachable(logged_in):
    client, account, _ = logged_in
    other = Account.objects.create(company_name="Other")
    theirs = ContactList.objects.create(account=other, name="Theirs")
    assert client.get(f"/contacts/lists/{theirs.pk}/").status_code == 404
    assert client.post(f"/contacts/lists/{theirs.pk}/delete/").status_code == 404
    assert client.post(f"/contacts/lists/{theirs.pk}/add-messaged/").status_code == 404
    client.post(
        "/contacts/import/",
        {"mode": "paste", "list": theirs.pk, "numbers": "0977000111"},
    )
    assert theirs.contacts.count() == 0


@pytest.mark.django_db
def test_profile_consent_needs_attestation_and_opt_out_works(logged_in):
    client, account, user = logged_in
    contact = Contact.objects.create(account=account, phone="+260977000111")
    WhatsAppContact.objects.filter(contact=contact).delete()
    url = f"/contacts/{contact.public_id}/whatsapp-consent/"

    client.post(url, {"action": "opt_in"})
    assert WhatsAppContact.objects.get(contact=contact).opt_in_status == _Opt.UNKNOWN

    client.post(url, {"action": "opt_in", "attested": "1"})
    identity = WhatsAppContact.objects.get(contact=contact)
    assert identity.opt_in_status == _Opt.OPTED_IN
    assert identity.opt_in_source == f"owner_attestation:user:{user.pk}"

    client.post(url, {"action": "opt_out"})
    identity.refresh_from_db()
    assert identity.opt_in_status == _Opt.OPTED_OUT


@pytest.mark.django_db
def test_profile_add_and_remove_list_returns_to_profile(logged_in):
    client, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260977000111")
    clist = ContactList.objects.create(account=account, name="VIPs")
    profile = f"/contacts/{contact.public_id}/"

    resp = client.post(
        f"/contacts/lists/{clist.pk}/add-contact/",
        {"public_id": contact.public_id, "next": profile},
    )
    assert resp["Location"] == profile and clist.contacts.filter(pk=contact.pk).exists()
    body = client.get(profile).content.decode()
    assert "VIPs" in body and "Updates only" in body

    client.post(
        f"/contacts/lists/{clist.pk}/remove-contact/",
        {"public_id": contact.public_id, "next": profile},
    )
    assert not clist.contacts.filter(pk=contact.pk).exists()

"""Release 3 (audit 2026-10-19), phases 8–9: merging duplicates, deleting a
customer for good, export, notes, custom fields and the Customers page tools."""

import json

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from apps.accounts.models import Account, Membership
from apps.contacts.models import (
    Contact,
    ContactList,
    ContactNote,
    CustomAttributeDef,
    Tag,
)
from apps.whatsapp.models import WhatsAppContact


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


def _login(client, account, role=Membership.Role.OWNER, name="owner"):
    user = User.objects.create_user(name, f"{name}@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=role)
    client.force_login(user)
    return user


# --- Merge -----------------------------------------------------------------------


@pytest.mark.django_db
def test_merge_moves_everything_and_deletes_the_duplicate(account):
    from apps.contacts.merge import merge_contacts
    from apps.crm.models import Lead

    primary = Contact.objects.create(
        account=account, first_name="Ada", phone="+260977000001"
    )
    dup = Contact.objects.create(
        account=account,
        email="ada@example.com",
        last_name="Mwape",
        attributes={"area": "Lusaka"},
    )
    vips = ContactList.objects.create(account=account, name="VIPs")
    vips.contacts.add(primary, dup)  # on the list twice → once after
    other = ContactList.objects.create(account=account, name="Market")
    other.contacts.add(dup)
    tag = Tag.objects.create(account=account, name="VIP", slug="vip")
    dup.tags.add(tag)
    Lead.objects.create(account=account, contact=primary)
    dup_lead = Lead.objects.create(account=account, contact=dup)
    ig_model = __import__(
        "apps.instagram.models", fromlist=["InstagramContact"]
    ).InstagramContact
    ig = ig_model.objects.create(
        account=account, instagram_scoped_id="ig1", username="ada.m", contact=dup
    )
    ContactNote.objects.create(account=account, contact=dup, body="Likes chilli")

    result = merge_contacts(primary, dup)

    primary.refresh_from_db()
    assert not Contact.objects.filter(pk=dup.pk).exists()
    assert (primary.email, primary.last_name) == ("ada@example.com", "Mwape")
    assert primary.attributes["area"] == "Lusaka"
    assert set(primary.lists.values_list("name", flat=True)) == {"VIPs", "Market"}
    assert list(primary.tags.all()) == [tag]
    ig.refresh_from_db()
    assert ig.contact_id == primary.pk
    assert primary.notes.count() == 1
    dup_lead.refresh_from_db()
    assert dup_lead.contact_id == primary.pk and dup_lead.status == Lead.Status.LOST
    assert result.closed_leads == 1
    assert primary.events.filter(type="contact.merged").exists()


@pytest.mark.django_db
def test_merge_refuses_across_businesses(account):
    from apps.contacts.merge import MergeError, merge_contacts

    a = Contact.objects.create(account=account, phone="+260977000001")
    b = Contact.objects.create(
        account=Account.objects.create(company_name="X"), phone="+260977000002"
    )
    with pytest.raises(MergeError):
        merge_contacts(a, b)


@pytest.mark.django_db
def test_suggestions_and_dismissing_them(account):
    from apps.contacts.merge import dismiss_suggestion, suggested_duplicates

    a = Contact.objects.create(
        account=account, first_name="Ada", last_name="Mwape", phone="+260977000001"
    )
    b = Contact.objects.create(account=account, first_name="ada", last_name="MWAPE")
    assert suggested_duplicates(a) == [b]
    dismiss_suggestion(a, b)
    a.refresh_from_db()
    b.refresh_from_db()
    assert suggested_duplicates(a) == [] and suggested_duplicates(b) == []


@pytest.mark.django_db
def test_merge_screen(client, account):
    _login(client, account)
    a = Contact.objects.create(account=account, first_name="Ada", phone="+260977000001")
    b = Contact.objects.create(
        account=account, first_name="Ada", email="ada@example.com"
    )
    body = client.get(
        f"/contacts/{a.public_id}/merge/?with={b.public_id}"
    ).content.decode()
    assert "Keep this one" in body
    resp = client.post(
        f"/contacts/{a.public_id}/merge/", {"other": b.public_id, "keep": "other"}
    )
    assert resp["Location"] == f"/contacts/{b.public_id}/"
    assert not Contact.objects.filter(pk=a.pk).exists()
    b.refresh_from_db()
    assert b.phone == "+260977000001"


@pytest.mark.django_db
def test_emails_are_unique_ignoring_case(account):
    from django.db import IntegrityError, transaction

    from apps.contacts.services import upsert_contact

    first = Contact.objects.create(account=account, email="Ada@Example.com")
    with pytest.raises(IntegrityError), transaction.atomic():
        Contact.objects.create(account=account, email="ada@example.com")
    found, created = upsert_contact(account, "ADA@example.COM", first_name="Ada")
    assert found.pk == first.pk and not created


@pytest.mark.django_db
def test_check_contact_emails_reports_clean_data(account, capsys):
    Contact.objects.create(account=account, email="a@example.com")
    call_command("check_contact_emails", "--merge")
    assert "0 group(s) found" in capsys.readouterr().out


# --- Delete ------------------------------------------------------------------------


@pytest.mark.django_db
def test_delete_customer_removes_whatsapp_instagram_and_queues_hubspot(account):
    from apps.contacts.erasure import delete_customer, preview
    from apps.hubspot.models import HubSpotContactLink, HubSpotSyncJob
    from apps.instagram.models import InstagramContact

    c = Contact.objects.create(account=account, phone="+260977000001")
    InstagramContact.objects.create(
        account=account, instagram_scoped_id="ig1", contact=c
    )
    HubSpotContactLink.objects.create(account=account, contact=c, hs_object_id="42")
    assert ("WhatsApp numbers", 1) in preview(c) or WhatsAppContact.objects.filter(
        contact=c
    ).count() == 0

    delete_customer(c)

    assert not Contact.objects.filter(pk=c.pk).exists()
    assert not WhatsAppContact.objects.filter(phone_number="+260977000001").exists()
    assert not InstagramContact.objects.filter(instagram_scoped_id="ig1").exists()
    job = HubSpotSyncJob.objects.get()
    assert (job.object_type, job.hs_object_id) == ("contact_delete", "42")


@pytest.mark.django_db
def test_only_admins_delete_and_it_needs_typing_delete(client, account):
    c = Contact.objects.create(account=account, phone="+260977000001")
    _login(client, account, role=Membership.Role.MEMBER, name="member")
    client.post(f"/contacts/{c.public_id}/delete/", {"confirm": "delete"})
    assert Contact.objects.filter(pk=c.pk).exists()

    _login(client, account, name="boss")
    assert (
        "What gets deleted"
        in client.get(f"/contacts/{c.public_id}/delete/").content.decode()
    )
    client.post(f"/contacts/{c.public_id}/delete/", {"confirm": "nope"})
    assert Contact.objects.filter(pk=c.pk).exists()
    resp = client.post(f"/contacts/{c.public_id}/delete/", {"confirm": "delete"})
    assert resp["Location"] == "/contacts/"
    assert not Contact.objects.filter(pk=c.pk).exists()


@pytest.mark.django_db
def test_bulk_tag_and_delete_follow_the_filter(client, account):
    _login(client, account)
    ada = Contact.objects.create(
        account=account, first_name="Ada", phone="+260977000001"
    )
    Contact.objects.create(account=account, first_name="Bwalya", phone="+260977000002")

    client.post(
        "/contacts/bulk/tag/",
        {"all_matching": "1", "filter": "q=Ada", "tag": "VIP", "action": "add"},
    )
    assert list(ada.tags.values_list("name", flat=True)) == ["VIP"]
    client.post(
        "/contacts/bulk/tag/",
        {"ids": [ada.public_id], "tag": "VIP", "action": "remove"},
    )
    assert not ada.tags.exists()

    client.post(
        "/contacts/bulk/delete/",
        {"all_matching": "1", "filter": "q=Ada", "confirm": "delete"},
    )
    assert list(Contact.objects.values_list("first_name", flat=True)) == ["Bwalya"]


# --- Export ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_export_is_admin_only_and_formula_safe(client, account):
    Contact.objects.create(account=account, first_name="=CMD()", phone="+260977000001")
    Contact.objects.create(
        account=Account.objects.create(company_name="X"), first_name="Theirs"
    )
    _login(client, account, role=Membership.Role.MEMBER, name="member")
    assert client.get("/contacts/export.csv").status_code == 302

    _login(client, account, name="boss")
    body = client.get("/contacts/export.csv").content.decode()
    assert "'=CMD()" in body and "+260977000001" in body and "Theirs" not in body


# --- Notes and custom fields ------------------------------------------------------------


@pytest.mark.django_db
def test_notes_on_the_profile(client, account):
    author = _login(client, account, role=Membership.Role.MEMBER, name="member")
    c = Contact.objects.create(account=account, phone="+260977000001")
    client.post(f"/contacts/{c.public_id}/notes/", {"body": "Prefers mornings"})
    note = ContactNote.objects.get()
    assert note.author == author
    assert (
        "Prefers mornings" in client.get(f"/contacts/{c.public_id}/").content.decode()
    )

    _login(client, account, role=Membership.Role.MEMBER, name="other")
    client.post(f"/contacts/{c.public_id}/notes/{note.pk}/delete/")
    assert ContactNote.objects.exists()  # not theirs, not an admin


@pytest.mark.django_db
def test_custom_fields_settings_and_profile_editing(client, account):
    _login(client, account)
    client.post(
        "/contacts/fields/",
        {"label": "Area", "type": "choice", "options": "Lusaka\nNdola"},
    )
    d = CustomAttributeDef.objects.get(account=account)
    assert d.key == "area" and [o["label"] for o in d.options] == ["Lusaka", "Ndola"]

    c = Contact.objects.create(account=account, phone="+260977000001")
    client.post(f"/contacts/{c.public_id}/fields/", {"attr__area": "ndola"})
    c.refresh_from_db()
    assert c.attributes["area"] == "ndola"
    assert "Ndola" in client.get(f"/contacts/{c.public_id}/").content.decode()

    client.post(f"/contacts/fields/{d.pk}/archive/")
    d.refresh_from_db()
    assert d.archived_at is not None


@pytest.mark.django_db
def test_create_custom_field_json_endpoint(client, account):
    _login(client, account)
    resp = client.post(
        "/contacts/custom-fields/create/",
        data=json.dumps({"label": "Favourite dish", "type": "string"}),
        content_type="application/json",
    )
    assert resp.json()["key"] == "favourite_dish"
    again = client.post(
        "/contacts/custom-fields/create/",
        data=json.dumps({"label": "Favourite dish", "type": "string"}),
        content_type="application/json",
    )
    assert again.json()["key"] == "favourite_dish"  # same field, not a copy
    bad = client.post(
        "/contacts/custom-fields/create/",
        data=json.dumps({"label": ""}),
        content_type="application/json",
    )
    assert bad.status_code == 400


# --- Customers page ---------------------------------------------------------------------


@pytest.mark.django_db
def test_customers_sort_by_name(client, account):
    _login(client, account)
    Contact.objects.create(account=account, first_name="Zed", phone="+260977000001")
    Contact.objects.create(account=account, first_name="Ada", phone="+260977000002")
    body = client.get("/contacts/?sort=name").content.decode()
    assert body.index("Ada") < body.index("Zed")
    body = client.get("/contacts/?sort=-name").content.decode()
    assert body.index("Zed") < body.index("Ada")


@pytest.mark.django_db
def test_profile_shows_duplicate_banner_and_activity(client, account):
    _login(client, account)
    a = Contact.objects.create(
        account=account, first_name="Ada", last_name="Mwape", phone="+260977000001"
    )
    Contact.objects.create(
        account=account, first_name="Ada", last_name="Mwape", email="a@example.com"
    )
    body = client.get(f"/contacts/{a.public_id}/").content.decode()
    assert "This might be the same person" in body
    assert "Activity" in body

"""Customer list management from the dashboard.

Until now a ContactList could only be created through the public API — the
dashboard's /contacts/ page had no way to make one, so
/whatsapp/campaigns/new/ (and the email campaign flow) always found an empty
list of lists no matter how many contacts existed. This is the minimal fix:
name a list from the dashboard, then add contacts to it by tag or by search.
"""

import pytest
from django.contrib.auth.models import User
from django.test import override_settings

from apps.accounts.models import Account, Membership
from apps.contacts import tags as contact_tags
from apps.contacts.models import Contact, ContactList

# The project only mounts /whatsapp/ when WHATSAPP_ENABLED (off by default in
# tests) — same fixture apps.whatsapp view tests use (test_registration.py).
_wa_urls = override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled")


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.mark.django_db
def test_list_index_shows_no_lists_message_when_empty(logged_in):
    client, _account, _ = logged_in
    resp = client.get("/contacts/lists/")
    assert resp.status_code == 200
    assert b"No customer lists yet" in resp.content


@pytest.mark.django_db
def test_create_list_with_no_seed_starts_empty(logged_in):
    client, account, _ = logged_in
    resp = client.post("/contacts/lists/create/", {"name": "VIP customers"})
    assert resp.status_code == 302
    contact_list = ContactList.objects.get(account=account, name="VIP customers")
    assert contact_list.contacts.count() == 0
    assert resp["Location"] == f"/contacts/lists/{contact_list.pk}/"


@pytest.mark.django_db
def test_create_list_rejects_duplicate_name(logged_in):
    client, account, _ = logged_in
    ContactList.objects.create(account=account, name="VIP customers")
    resp = client.post("/contacts/lists/create/", {"name": "VIP customers"})
    assert resp.status_code == 302
    assert resp["Location"] == "/contacts/lists/"
    assert (
        ContactList.objects.filter(account=account, name="VIP customers").count() == 1
    )


@pytest.mark.django_db
def test_create_list_seeded_from_tag_adds_matching_contacts(logged_in):
    client, account, _ = logged_in
    tagged = Contact.objects.create(account=account, phone="+260971000001")
    untagged = Contact.objects.create(account=account, phone="+260971000002")
    contact_tags.add_tag(tagged, "vip")

    resp = client.post("/contacts/lists/create/", {"name": "VIPs", "seed_tag": "vip"})
    assert resp.status_code == 302
    contact_list = ContactList.objects.get(account=account, name="VIPs")
    assert list(contact_list.contacts.all()) == [tagged]
    assert untagged not in contact_list.contacts.all()


@pytest.mark.django_db
def test_add_by_tag_only_adds_contacts_not_already_on_the_list(logged_in):
    client, account, _ = logged_in
    already_on = Contact.objects.create(account=account, phone="+260971000001")
    to_add = Contact.objects.create(account=account, phone="+260971000002")
    contact_tags.add_tag(already_on, "vip")
    contact_tags.add_tag(to_add, "vip")
    contact_list = ContactList.objects.create(account=account, name="VIPs")
    contact_list.contacts.add(already_on)

    resp = client.post(f"/contacts/lists/{contact_list.pk}/add-by-tag/", {"tag": "vip"})
    assert resp.status_code == 302
    assert set(contact_list.contacts.all()) == {already_on, to_add}


@pytest.mark.django_db
def test_add_by_tag_unknown_tag_shows_error_and_does_not_crash(logged_in):
    client, account, _ = logged_in
    contact_list = ContactList.objects.create(account=account, name="VIPs")
    resp = client.post(
        f"/contacts/lists/{contact_list.pk}/add-by-tag/", {"tag": "nonexistent"}
    )
    assert resp.status_code == 302
    assert contact_list.contacts.count() == 0


@pytest.mark.django_db
def test_add_single_contact_by_search(logged_in):
    client, account, _ = logged_in
    contact = Contact.objects.create(
        account=account, phone="+260971000001", first_name="Ada"
    )
    contact_list = ContactList.objects.create(account=account, name="VIPs")

    resp = client.get(f"/contacts/lists/{contact_list.pk}/?cq=Ada")
    assert resp.status_code == 200
    assert b"Ada" in resp.content

    resp = client.post(
        f"/contacts/lists/{contact_list.pk}/add-contact/",
        {"public_id": contact.public_id},
    )
    assert resp.status_code == 302
    assert contact in contact_list.contacts.all()


@pytest.mark.django_db
def test_search_candidates_exclude_existing_members(logged_in):
    client, account, _ = logged_in
    member = Contact.objects.create(
        account=account, phone="+260971000001", first_name="Ada Member"
    )
    contact_list = ContactList.objects.create(account=account, name="VIPs")
    contact_list.contacts.add(member)

    resp = client.get(f"/contacts/lists/{contact_list.pk}/?cq=Ada")
    assert resp.status_code == 200
    # Already a member: shown in the member table, not offered again as a candidate.
    assert b"No match, or they" in resp.content


@pytest.mark.django_db
def test_remove_contact_from_list(logged_in):
    client, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260971000001")
    contact_list = ContactList.objects.create(account=account, name="VIPs")
    contact_list.contacts.add(contact)

    resp = client.post(
        f"/contacts/lists/{contact_list.pk}/remove-contact/",
        {"public_id": contact.public_id},
    )
    assert resp.status_code == 302
    assert contact not in contact_list.contacts.all()


@pytest.mark.django_db
def test_list_scoped_to_account(logged_in):
    client, account, _ = logged_in
    other_account = Account.objects.create(company_name="Other Co")
    other_list = ContactList.objects.create(account=other_account, name="Their list")

    resp = client.get(f"/contacts/lists/{other_list.pk}/")
    assert resp.status_code == 404


@_wa_urls
@pytest.mark.django_db
def test_new_contact_list_flows_into_campaign_creation_list(logged_in):
    """The bug this closes: /whatsapp/campaigns/new/ always said "no customer
    lists" because nothing created a ContactList from the dashboard."""
    from apps.whatsapp.models import MessageTemplate

    client, account, _ = logged_in
    MessageTemplate.objects.create(
        account=account,
        name="Promo",
        whatsapp_template_name="promo",
        content="Hi!",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )
    contact = Contact.objects.create(account=account, phone="+260971000001")
    client.post("/contacts/lists/create/", {"name": "Launch list"})
    contact_list = ContactList.objects.get(account=account, name="Launch list")
    client.post(
        f"/contacts/lists/{contact_list.pk}/add-contact/",
        {"public_id": contact.public_id},
    )

    resp = client.get("/whatsapp/campaigns/new/")
    assert resp.status_code == 200
    assert b"You don't have any customer lists yet" not in resp.content
    assert b"Launch list" in resp.content

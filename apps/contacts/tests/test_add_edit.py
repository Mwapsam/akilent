"""Adding and editing a customer by hand.

Until R3 contacts could only arrive via the API, a CSV import or an inbound
message, so a business owner couldn't add someone they met offline or correct
a misspelled name.
"""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account, user


@pytest.mark.django_db
def test_add_customer_by_phone(logged_in):
    client, account, _ = logged_in
    resp = client.post("/contacts/create/", {
        "first_name": "Ada", "last_name": "Mwape", "phone": "0971234567",
    })
    assert resp.status_code == 302
    contact = Contact.objects.get(account=account, first_name="Ada")
    # Stored E.164 so it matches the number WhatsApp reports for the same person.
    assert contact.phone == "+260971234567"
    assert resp["Location"] == f"/contacts/{contact.public_id}/"


@pytest.mark.django_db
def test_add_customer_by_email_only(logged_in):
    client, account, _ = logged_in
    client.post("/contacts/create/", {"first_name": "Ada", "email": "Ada@Example.com"})
    contact = Contact.objects.get(account=account, first_name="Ada")
    assert contact.email == "ada@example.com"
    assert contact.phone is None


@pytest.mark.django_db
def test_add_customer_needs_a_way_to_reach_them(logged_in):
    client, account, _ = logged_in
    resp = client.post("/contacts/create/", {"first_name": "Ada"}, follow=True)
    assert resp.status_code == 200
    assert Contact.objects.filter(account=account).count() == 0


@pytest.mark.django_db
def test_add_customer_rejects_an_unusable_phone(logged_in):
    client, account, _ = logged_in
    client.post("/contacts/create/", {"first_name": "Ada", "phone": "nope"}, follow=True)
    assert Contact.objects.filter(account=account).count() == 0


@pytest.mark.django_db
def test_adding_an_existing_number_updates_instead_of_duplicating(logged_in):
    client, account, _ = logged_in
    Contact.objects.create(account=account, phone="+260971234567")

    client.post("/contacts/create/", {
        "first_name": "Ada", "phone": "+260971234567",
    })
    assert Contact.objects.filter(account=account).count() == 1
    assert Contact.objects.get(account=account).first_name == "Ada"


@pytest.mark.django_db
def test_edit_customer(logged_in):
    client, account, _ = logged_in
    contact = Contact.objects.create(account=account, phone="+260971234567", first_name="Ade")

    resp = client.post(f"/contacts/{contact.public_id}/edit/", {
        "first_name": "Ada", "last_name": "Mwape",
        "phone": "+260971234567", "email": "ada@example.com",
    })
    assert resp.status_code == 302
    contact.refresh_from_db()
    assert (contact.first_name, contact.last_name) == ("Ada", "Mwape")
    assert contact.email == "ada@example.com"


@pytest.mark.django_db
def test_edit_refuses_a_number_another_customer_already_has(logged_in):
    client, account, _ = logged_in
    Contact.objects.create(account=account, phone="+260970000000")
    contact = Contact.objects.create(account=account, phone="+260971234567")

    client.post(f"/contacts/{contact.public_id}/edit/", {"phone": "+260970000000"}, follow=True)
    contact.refresh_from_db()
    assert contact.phone == "+260971234567"


@pytest.mark.django_db
def test_edit_scoped_to_account(logged_in):
    client, _, _ = logged_in
    other = Account.objects.create(company_name="Other Co")
    theirs = Contact.objects.create(account=other, phone="+260970000000")

    resp = client.post(f"/contacts/{theirs.public_id}/edit/", {"phone": "+260979999999"})
    assert resp.status_code == 404
    theirs.refresh_from_db()
    assert theirs.phone == "+260970000000"

"""Phone-only contacts (e.g. WhatsApp-first customers) must be visible and findable."""
import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.contacts.services import upsert_contact_by_phone


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, acc


@pytest.mark.django_db
def test_phone_only_contact_shows_its_number_not_none(logged_in):
    client, acc = logged_in
    upsert_contact_by_phone(acc, "+260971903744", source="whatsapp")

    body = client.get("/contacts/").content.decode()
    assert "+260971903744" in body
    assert ">None<" not in body


@pytest.mark.django_db
def test_search_finds_contacts_by_phone_and_name(logged_in):
    client, acc = logged_in
    contact, _ = upsert_contact_by_phone(acc, "+260971903744", source="whatsapp")
    contact.first_name = "Tester"
    contact.save()
    upsert_contact_by_phone(acc, "+260977000111", source="whatsapp")

    by_phone = client.get("/contacts/", {"q": "903744"}).content.decode()
    assert "+260971903744" in by_phone and "+260977000111" not in by_phone
    by_name = client.get("/contacts/", {"q": "tester"}).content.decode()
    assert "+260971903744" in by_name and "+260977000111" not in by_name


@pytest.mark.django_db
def test_profile_renders_for_a_phone_only_contact(logged_in):
    client, acc = logged_in
    contact, _ = upsert_contact_by_phone(acc, "+260971903744", source="whatsapp")

    resp = client.get(f"/contacts/{contact.public_id}/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "+260971903744" in body
    assert "<h1" in body and "None" not in body.split("<h1", 1)[1].split("</p>", 1)[0]

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.contacts.services import record_contact_event, upsert_contact


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, acc


@pytest.mark.django_db
def test_contact_list_and_profile_render(logged_in):
    client, acc = logged_in
    c, _ = upsert_contact(acc, "a@x.com", first_name="Ada")
    record_contact_event(c, "email.opened")

    resp = client.get("/contacts/")
    assert resp.status_code == 200
    assert "a@x.com" in resp.content.decode()

    detail = client.get(f"/contacts/{c.public_id}/")
    assert detail.status_code == 200
    body = detail.content.decode()
    assert "Activity" in body and "email.opened" in body


@pytest.mark.django_db
def test_profile_scoped_to_account(logged_in):
    client, _ = logged_in
    other = Account.objects.create(company_name="Other")
    c, _ = upsert_contact(other, "x@y.com")
    assert client.get(f"/contacts/{c.public_id}/").status_code == 404

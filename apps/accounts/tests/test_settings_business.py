"""Settings → Business: the mailing address printed in campaign footers."""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership

ADDRESS = {
    "legal_name": "Acme Holdings Ltd",
    "address_line1": "12 Cairo Road",
    "address_line2": "",
    "city": "Lusaka",
    "state_region": "",
    "postal_code": "10101",
    "country": "Zambia",
}


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


def _login(client, account, role):
    user = User.objects.create_user(role, f"{role}@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=role)
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_page_explains_why_and_prompts_when_missing(client, account):
    _login(client, account, Membership.Role.OWNER)
    resp = client.get("/settings/business/")
    assert resp.status_code == 200
    assert b"as anti-spam law requires" in resp.content
    assert b"Add your mailing address to send email campaigns" in resp.content


@pytest.mark.django_db
def test_owner_saves_address_and_unlocks_campaigns(client, account):
    _login(client, account, Membership.Role.OWNER)
    assert account.has_postal_address is False

    resp = client.post("/settings/business/", ADDRESS)
    assert resp.status_code == 302

    account.refresh_from_db()
    assert account.address_line1 == "12 Cairo Road"
    assert account.legal_name == "Acme Holdings Ltd"
    assert account.has_postal_address is True

    # The page now previews the footer exactly as recipients will see it.
    page = client.get("/settings/business/").content.decode()
    assert "Acme Holdings Ltd · 12 Cairo Road, Lusaka, 10101, Zambia" in page


@pytest.mark.django_db
def test_admin_can_edit_too(client, account):
    _login(client, account, Membership.Role.ADMIN)
    assert client.post("/settings/business/", ADDRESS).status_code == 302
    account.refresh_from_db()
    assert account.has_postal_address is True


@pytest.mark.django_db
def test_member_cannot_edit(client, account):
    _login(client, account, Membership.Role.MEMBER)
    page = client.get("/settings/business/")
    assert page.status_code == 200
    assert b"Only an owner or admin can change business details." in page.content

    client.post("/settings/business/", ADDRESS)
    account.refresh_from_db()
    assert account.address_line1 == ""


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["address_line1", "city", "country"])
def test_minimum_fields_are_required(client, account, field):
    _login(client, account, Membership.Role.OWNER)
    resp = client.post("/settings/business/", {**ADDRESS, field: ""})
    assert resp.status_code == 200  # re-rendered with the error
    account.refresh_from_db()
    assert account.has_postal_address is False


@pytest.mark.django_db
def test_whitespace_only_is_rejected(client, account):
    """Would pass a naive `required` check but render an empty footer."""
    _login(client, account, Membership.Role.OWNER)
    client.post("/settings/business/", {**ADDRESS, "city": "   "})
    account.refresh_from_db()
    assert account.has_postal_address is False


@pytest.mark.django_db
def test_tab_is_linked_from_settings(client, account):
    _login(client, account, Membership.Role.OWNER)
    assert b'href="/settings/business/"' in client.get("/settings/").content


# ---- country is a select ----------------------------------------------------------------------


@pytest.mark.django_db
def test_country_is_a_select_showing_the_saved_country(client, account):
    Account.objects.filter(pk=account.pk).update(country="ZM")  # stored as a code
    _login(client, account, Membership.Role.OWNER)
    body = client.get("/settings/business/").content.decode()
    assert '<select id="id_country" name="country"' in body
    assert '<option value="Zambia" selected>Zambia</option>' in body


@pytest.mark.django_db
def test_an_old_typed_country_is_kept_when_saving(client, account):
    Account.objects.filter(pk=account.pk).update(country="Republic of Somewhere")
    _login(client, account, Membership.Role.OWNER)
    body = client.get("/settings/business/").content.decode()
    assert "Republic of Somewhere</option>" in body
    client.post("/settings/business/", {**ADDRESS, "country": "Republic of Somewhere"})
    account.refresh_from_db()
    assert account.country == "Republic of Somewhere"
    assert account.has_postal_address


@pytest.mark.django_db
def test_a_country_not_on_the_list_is_refused(client, account):
    _login(client, account, Membership.Role.OWNER)
    resp = client.post("/settings/business/", {**ADDRESS, "country": "Atlantis"})
    assert resp.status_code == 200
    account.refresh_from_db()
    assert account.country == ""


def test_country_name_reads_codes_and_any_case():
    from apps.accounts.forms import country_name

    assert country_name("zm") == country_name("zambia") == "Zambia"
    assert country_name("United Kingdom") == "United Kingdom"
    assert country_name("Somewhere") == "Somewhere"
    assert country_name("") == ""

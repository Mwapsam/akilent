"""Settings › API keys: named keys with scopes, test mode and expiry, revoked one at a time.

Covers the page (owner vs member), what the created key can do against the real API
authentication, expiry, revoking, and that the Email/WhatsApp shortcuts no longer wipe
every key on the account.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.core.models import PlatformAuditLog
from apps.email.models import EmailApiKey

URL = "/settings/api-keys/"


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


def _login(client, account, role):
    user = User.objects.create_user(role, f"{role}@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=role)
    client.force_login(user)
    return user


@pytest.fixture
def owner(client, account):
    _login(client, account, Membership.Role.OWNER)
    return client


def _create(client, **data):
    data = {"name": "Online shop", "scopes": ["messages:send"], **data}
    return client.post(URL, data)


@pytest.mark.django_db
def test_owner_creates_a_named_key_with_scopes_and_sees_it_once(owner, account):
    _create(owner, scopes=["messages:send", "templates:manage"], mode="test")
    key = EmailApiKey.objects.get(account=account)
    assert key.name == "Online shop" and key.mode == "test"
    assert key.scopes == ["messages:send", "templates:manage"]
    assert key.expires_at is None

    first = owner.get(URL).content.decode()
    assert "ak_test_" in first and "Copy your key now" in first
    again = owner.get(URL).content.decode()
    assert "Copy your key now" not in again and f"••••{key.last4}" in again
    assert "Manage email templates" in again
    assert PlatformAuditLog.objects.filter(action="api_key.created").exists()


@pytest.mark.django_db
def test_unknown_scopes_are_dropped_and_a_key_needs_a_name_and_a_scope(owner, account):
    _create(owner, scopes=["messages:send", "admin:everything"])
    assert EmailApiKey.objects.get(account=account).scopes == ["messages:send"]
    _create(owner, name="  ")
    _create(owner, scopes=["admin:everything"])
    assert EmailApiKey.objects.filter(account=account).count() == 1


@pytest.mark.django_db
def test_expiry_is_set_and_an_expired_key_stops_authenticating(owner, account):
    _create(owner, expires_in="30")
    key = EmailApiKey.objects.get(account=account)
    assert key.expires_at - timezone.now() > timedelta(days=29)

    _, raw = EmailApiKey.create_for_account(account, name="old")
    assert EmailApiKey.authenticate(raw) is not None
    EmailApiKey.objects.filter(name="old").update(
        expires_at=timezone.now() - timedelta(minutes=1)
    )
    assert EmailApiKey.authenticate(raw) is None


@pytest.mark.django_db
def test_revoking_one_key_leaves_the_others_working(owner, account):
    a, raw_a = EmailApiKey.create_for_account(account, name="Shop")
    _, raw_b = EmailApiKey.create_for_account(account, name="App")
    owner.post(f"{URL}{a.pk}/revoke/")
    assert EmailApiKey.authenticate(raw_a) is None
    assert EmailApiKey.authenticate(raw_b) is not None
    html = owner.get(URL).content.decode()
    assert 'aria-label="Revoke Shop"' not in html and 'aria-label="Revoke App"' in html
    assert PlatformAuditLog.objects.filter(action="api_key.revoked").exists()


@pytest.mark.django_db
def test_a_member_sees_keys_but_cannot_create_or_revoke(client, account):
    key, raw = EmailApiKey.create_for_account(account, name="Shop")
    _login(client, account, Membership.Role.MEMBER)
    html = client.get(URL).content.decode()
    assert "Shop" in html and "Create key" not in html
    _create(client)
    client.post(f"{URL}{key.pk}/revoke/")
    assert EmailApiKey.objects.filter(account=account).count() == 1
    assert EmailApiKey.authenticate(raw) is not None


@pytest.mark.django_db
def test_another_business_cannot_revoke_your_key(owner, account):
    other = Account.objects.create(company_name="Other")
    theirs, raw = EmailApiKey.create_for_account(other, name="Theirs")
    assert owner.post(f"{URL}{theirs.pk}/revoke/").status_code == 404
    assert EmailApiKey.authenticate(raw) is not None


@pytest.mark.django_db
def test_the_email_shortcut_makes_only_the_first_key_and_only_for_admins(
    client, account
):
    existing, raw = EmailApiKey.create_for_account(account, name="Shop")
    _login(client, account, Membership.Role.MEMBER)
    client.post("/email/keys/create/")
    assert EmailApiKey.objects.filter(account=account).count() == 1

    client.logout()
    _login(client, account, Membership.Role.OWNER)
    resp = client.post("/email/keys/create/")
    assert resp.status_code == 302 and resp.url == URL
    assert EmailApiKey.authenticate(raw) is not None  # no longer wiped
    assert EmailApiKey.objects.filter(account=account).count() == 1

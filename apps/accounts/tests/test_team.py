import pytest
from django.contrib.auth.models import User
from django.core import mail

from apps.accounts.models import Account, Invitation, Membership


@pytest.fixture
def owner(db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user, account


@pytest.fixture
def owner_client(client, owner):
    user, _ = owner
    client.force_login(user)
    return client


# --- Profile & security -------------------------------------------------------

@pytest.mark.django_db
def test_profile_update(owner_client, owner):
    user, _ = owner
    resp = owner_client.post("/settings/", {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"})
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.first_name == "Ada"
    assert user.email == "ada@example.com"


@pytest.mark.django_db
def test_profile_rejects_duplicate_email(owner_client, owner):
    User.objects.create_user("other", "taken@example.com", "x")
    resp = owner_client.post("/settings/", {"first_name": "", "last_name": "", "email": "taken@example.com"})
    assert resp.status_code == 200  # re-rendered with error
    assert b"Another account already uses this email." in resp.content


@pytest.mark.django_db
def test_password_change_keeps_user_logged_in(owner_client, owner):
    resp = owner_client.post("/settings/security/", {
        "old_password": "Sup3r-secret-pw",
        "new_password1": "An0ther-secret-pw",
        "new_password2": "An0ther-secret-pw",
    })
    assert resp.status_code == 302
    user, _ = owner
    user.refresh_from_db()
    assert user.check_password("An0ther-secret-pw")
    # Still authenticated (session hash was rotated in place).
    assert owner_client.get("/settings/").status_code == 200


@pytest.mark.django_db
def test_settings_pages_render(owner_client):
    assert owner_client.get("/settings/").status_code == 200
    assert owner_client.get("/settings/security/").status_code == 200
    assert owner_client.get("/settings/team/").status_code == 200
    assert owner_client.get("/settings/tools/").status_code == 200


# --- Optional tools (R1.5a) ----------------------------------------------------

@pytest.mark.django_db
def test_optional_tools_default_to_enabled(owner_client, owner):
    """No ModuleSubscription row yet — module_enabled()'s "no row = enabled" rule
    means both checkboxes must render checked, not unchecked."""
    resp = owner_client.get("/settings/tools/")
    body = resp.content.decode()
    assert body.count('name="crm" checked') == 1
    assert body.count('name="commerce" checked') == 1


@pytest.mark.django_db
def test_optional_tools_can_be_turned_off_and_back_on(owner_client, owner):
    from apps.billing import api as billing_api

    _, account = owner
    # Unchecking a box means it's simply absent from POST data.
    owner_client.post("/settings/tools/", {"crm": "on"})  # commerce omitted = off
    assert billing_api.module_enabled(account, "crm") is True
    assert billing_api.module_enabled(account, "commerce") is False

    owner_client.post("/settings/tools/", {"crm": "on", "commerce": "on"})
    assert billing_api.module_enabled(account, "commerce") is True


@pytest.mark.django_db
def test_accept_register_page_renders_for_new_email(client, owner):
    _, account = owner
    invite = Invitation.objects.create(account=account, email="brand-new@example.com", role="admin")
    resp = client.get(f"/invite/{invite.token}/")
    assert resp.status_code == 200
    assert b"Create account" in resp.content


# --- Invitations --------------------------------------------------------------

@pytest.mark.django_db
def test_owner_can_invite_and_email_is_sent(owner_client, owner):
    _, account = owner
    resp = owner_client.post("/settings/team/invite/", {"email": "new@example.com", "role": "member"})
    assert resp.status_code == 302
    invite = Invitation.objects.get(email="new@example.com")
    assert invite.account == account
    assert invite.role == "member"
    assert len(mail.outbox) == 1
    assert invite.token in mail.outbox[0].body


@pytest.mark.django_db
def test_reinviting_same_email_updates_not_duplicates(owner_client, owner):
    owner_client.post("/settings/team/invite/", {"email": "dup@example.com", "role": "member"})
    owner_client.post("/settings/team/invite/", {"email": "dup@example.com", "role": "admin"})
    invites = Invitation.objects.filter(email="dup@example.com", accepted_at__isnull=True)
    assert invites.count() == 1
    assert invites.first().role == "admin"


@pytest.mark.django_db
def test_member_cannot_invite(client, owner):
    _, account = owner
    member = User.objects.create_user("member", "m@example.com", "Sup3r-secret-pw")
    Membership.objects.create(user=member, account=account, role=Membership.Role.MEMBER)
    client.force_login(member)
    resp = client.post("/settings/team/invite/", {"email": "x@example.com", "role": "member"})
    assert resp.status_code == 302
    assert not Invitation.objects.filter(email="x@example.com").exists()


@pytest.mark.django_db
def test_accept_invitation_new_user(client, owner):
    _, account = owner
    invite = Invitation.objects.create(account=account, email="join@example.com", role="member")
    resp = client.post(f"/invite/{invite.token}/", {
        "username": "joiner",
        "password1": "Sup3r-secret-pw",
        "password2": "Sup3r-secret-pw",
    })
    assert resp.status_code == 302
    user = User.objects.get(username="joiner")
    assert user.is_active and user.email == "join@example.com"
    assert Membership.objects.filter(user=user, account=account, role="member").exists()
    invite.refresh_from_db()
    assert invite.is_accepted


@pytest.mark.django_db
def test_accept_invitation_existing_user_redirects_to_signin(client, owner):
    _, account = owner
    User.objects.create_user("existing", "exists@example.com", "Sup3r-secret-pw")
    invite = Invitation.objects.create(account=account, email="exists@example.com", role="member")
    resp = client.get(f"/invite/{invite.token}/")
    assert resp.status_code == 200
    assert b"Sign in to accept" in resp.content


@pytest.mark.django_db
def test_expired_invitation_is_rejected(client, owner):
    from django.utils import timezone
    from datetime import timedelta

    _, account = owner
    invite = Invitation.objects.create(account=account, email="late@example.com", role="member")
    Invitation.objects.filter(pk=invite.pk).update(
        created_at=timezone.now() - timedelta(days=Invitation.EXPIRY_DAYS + 1)
    )
    resp = client.get(f"/invite/{invite.token}/")
    assert resp.status_code == 400
    assert b"not valid" in resp.content


# --- Member management --------------------------------------------------------

@pytest.mark.django_db
def test_owner_changes_member_role_and_removes(owner_client, owner):
    _, account = owner
    target_user = User.objects.create_user("t", "t@example.com", "x")
    m = Membership.objects.create(user=target_user, account=account, role=Membership.Role.MEMBER)

    owner_client.post(f"/settings/team/members/{m.pk}/role/", {"role": "admin"})
    m.refresh_from_db()
    assert m.role == "admin"

    owner_client.post(f"/settings/team/members/{m.pk}/remove/")
    assert not Membership.objects.filter(pk=m.pk).exists()


@pytest.mark.django_db
def test_owner_membership_cannot_be_removed(owner_client, owner):
    user, account = owner
    owner_m = Membership.objects.get(user=user, account=account)
    owner_client.post(f"/settings/team/members/{owner_m.pk}/remove/")
    assert Membership.objects.filter(pk=owner_m.pk).exists()

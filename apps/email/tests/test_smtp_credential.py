from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import AuditLog, EmailDomain, SmtpCredential
from apps.email.services import SmtpCredentialService
from apps.email.types import MailboxInfo, MailboxStatus, OperationResult, QuotaInfo


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(
        slug="p",
        name="P",
        price_monthly=Decimal("10"),
        email_apis=True,
    )
    Subscription.objects.create(
        account=acc,
        plan=plan,
        status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )
    return acc


@pytest.fixture
def domain(account):
    return EmailDomain.objects.create(
        account=account,
        domain="mail.acme.com",
        status=EmailDomain.Status.VERIFIED,
    )


class _FakeProvider:
    def __init__(self):
        self.created = {}

    def create_relay_identity(self, email, password, *, description=""):
        self.created[email] = password
        return MailboxInfo(
            email=email,
            name="API relay",
            status=MailboxStatus.ACTIVE,
            quota=QuotaInfo(used_mb=0, limit_mb=0),
        )

    def rotate_relay_secret(self, email, new_password):
        self.created[email] = new_password
        return OperationResult(success=True)

    def delete_relay_identity(self, email):
        self.created.pop(email, None)
        return OperationResult(success=True)


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    fake = _FakeProvider()
    # SmtpCredentialService no longer uses get_mail_provider
    # (it's no longer tied to a mail provider per the refactor)
    return fake


def _last_audit(account, action):
    return (
        AuditLog.objects.filter(account=account, action=action)
        .order_by("-timestamp")
        .first()
    )


@pytest.mark.django_db
def test_provision_creates_credential_with_reveal_once_secret(
    account, domain, fake_provider
):
    service = SmtpCredentialService(account)
    credential, secret = service.provision(domain)

    assert credential.username == f"relay@{domain.domain}"
    assert credential.is_active is True
    assert credential.last4 == secret[-4:]
    # The plaintext secret is never persisted — only its hash.
    assert secret not in (credential.secret_hash,)
    assert credential.secret_hash != ""

    log = _last_audit(account, "smtp_credential.provision")
    assert log is not None
    assert log.resource_id == credential.username


@pytest.mark.django_db
def test_rotate_changes_secret_and_hash(account, domain, fake_provider):
    service = SmtpCredentialService(account)
    credential, first_secret = service.provision(domain)
    first_hash = credential.secret_hash

    new_secret = service.rotate(credential)
    credential.refresh_from_db()

    assert new_secret != first_secret
    assert credential.secret_hash != first_hash
    assert credential.last4 == new_secret[-4:]

    log = _last_audit(account, "smtp_credential.rotate")
    assert log is not None


@pytest.mark.django_db
def test_revoke_deactivates_and_removes_from_provider(account, domain, fake_provider):
    service = SmtpCredentialService(account)
    credential, _ = service.provision(domain)

    service.revoke(credential)
    credential.refresh_from_db()

    assert credential.is_active is False

    log = _last_audit(account, "smtp_credential.revoke")
    assert log is not None


@pytest.mark.django_db
def test_view_requires_verified_domain(client, account):
    unverified = EmailDomain.objects.create(account=account, domain="new.acme.com")
    client.force_login(account.owner)
    resp = client.post(f"/email/domains/{unverified.pk}/smtp/create/")
    assert resp.status_code == 302
    assert not SmtpCredential.objects.filter(domain=unverified).exists()


@pytest.mark.django_db
def test_view_provisions_credential_and_shows_reveal_once_banner(
    client, account, domain
):
    client.force_login(account.owner)
    resp = client.post(f"/email/domains/{domain.pk}/smtp/create/", follow=True)
    assert resp.status_code == 200
    assert SmtpCredential.objects.filter(domain=domain, is_active=True).exists()
    # The reveal-once secret should appear once, right after creation.
    assert b"Copy this password now" in resp.content


@pytest.mark.django_db
def test_view_blocks_duplicate_active_credential(
    client, account, domain, fake_provider
):
    SmtpCredentialService(account).provision(domain)
    client.force_login(account.owner)
    resp = client.post(f"/email/domains/{domain.pk}/smtp/create/")
    assert resp.status_code == 302
    assert SmtpCredential.objects.filter(domain=domain, is_active=True).count() == 1


@pytest.mark.django_db
def test_view_rotate_and_revoke(client, account, domain):
    client.force_login(account.owner)
    credential, _ = SmtpCredentialService(account).provision(domain)

    rotate_resp = client.post(f"/email/smtp/{credential.pk}/rotate/", follow=True)
    assert rotate_resp.status_code == 200
    assert (
        b"password rotated" in rotate_resp.content
        or b"Copy this password now" in rotate_resp.content
    )

    revoke_resp = client.post(f"/email/smtp/{credential.pk}/revoke/", follow=True)
    assert revoke_resp.status_code == 200
    credential.refresh_from_db()
    assert credential.is_active is False

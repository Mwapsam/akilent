"""Audit-log coverage for domain lifecycle operations via DomainService.

The mailbox/alias tests that used to live here were removed with the Mailbox
and EmailAlias models; the product now provisions sending domains only.
"""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import Plan, Subscription
from apps.email.models import AuditLog, EmailDomain
from apps.email.services.domain import DomainService
from apps.email.types import DomainInfo, DomainStatus, OperationResult


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    plan = Plan.objects.create(
        slug="p",
        name="P",
        price_monthly=Decimal("10"),
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
    """Duck-typed EmailProvider — no HTTP / AWS calls."""

    def create_domain(self, domain, **kwargs):
        return DomainInfo(domain=domain, status=DomainStatus.PENDING)

    def set_domain_active(self, domain, *, active):
        return OperationResult(success=True)

    def delete_domain(self, domain):
        return OperationResult(success=True)


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr("apps.email.services.domain.get_mail_provider", lambda: fake)
    return fake


def _last_audit(account, action):
    return (
        AuditLog.objects.filter(account=account, action=action)
        .order_by("-timestamp")
        .first()
    )


@pytest.mark.django_db
def test_provision_writes_audit_log(account):
    d = EmailDomain.objects.create(account=account, domain="new.acme.com")
    DomainService(account, actor=account.owner).provision(d)
    log = _last_audit(account, "domain.provision")
    assert log is not None
    assert log.resource_id == "new.acme.com"
    assert log.success is True


@pytest.mark.django_db
def test_disable_and_enable_write_audit_logs(account, domain):
    svc = DomainService(account, actor=account.owner)
    svc.disable(domain)
    svc.enable(domain)
    assert _last_audit(account, "domain.disable") is not None
    assert _last_audit(account, "domain.enable") is not None
    domain.refresh_from_db()
    assert domain.is_active is True


@pytest.mark.django_db
def test_deprovision_writes_audit_log(account, domain):
    DomainService(account, actor=account.owner).deprovision(domain)
    log = _last_audit(account, "domain.deprovision")
    assert log is not None
    assert log.resource_id == domain.domain


@pytest.mark.django_db
def test_provision_failure_writes_failed_audit(account, monkeypatch):
    from apps.email.exceptions import EmailProviderError

    class _Boom(_FakeProvider):
        def create_domain(self, domain, **kwargs):
            raise EmailProviderError("provider exploded")

    monkeypatch.setattr("apps.email.services.domain.get_mail_provider", lambda: _Boom())
    d = EmailDomain.objects.create(account=account, domain="boom.acme.com")

    with pytest.raises(EmailProviderError):
        DomainService(account, actor=account.owner).provision(d)

    log = _last_audit(account, "domain.provision")
    assert log is not None
    assert log.success is False


@pytest.mark.django_db
def test_domain_toggle_view_writes_audit_log(client, account, domain):
    client.force_login(account.owner)
    resp = client.post(f"/email/domains/{domain.pk}/toggle/")
    assert resp.status_code == 302
    log = _last_audit(account, "domain.disable")
    assert log is not None
    assert log.resource_id == domain.domain
    assert log.actor_id == account.owner.pk


@pytest.mark.django_db
def test_domain_delete_view_writes_audit_log(client, account, domain):
    client.force_login(account.owner)
    resp = client.post(f"/email/domains/{domain.pk}/delete/")
    assert resp.status_code == 302
    log = _last_audit(account, "domain.deprovision")
    assert log is not None
    assert log.resource_id == domain.domain

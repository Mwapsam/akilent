"""Domains list is a list; domain management lives on a detail page."""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.email.models import EmailDomain
from apps.email.types import DomainInfo, DomainStatus


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.fixture
def domain(account):
    return EmailDomain.objects.create(
        account=account,
        domain="mail.acme.com",
        status=EmailDomain.Status.VERIFIED,
    )


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch):
    class _P:
        def create_domain(self, d, **kw):
            return DomainInfo(domain=d, status=DomainStatus.PENDING)

        def set_domain_active(self, d, *, active):
            from apps.email.types import OperationResult

            return OperationResult(success=True)

        def delete_domain(self, d):
            from apps.email.types import OperationResult

            return OperationResult(success=True)

    monkeypatch.setattr("apps.email.services.domain.get_mail_provider", lambda: _P())


@pytest.mark.django_db
def test_list_shows_rows_that_link_to_detail(client, account, domain):
    client.force_login(account.owner)
    resp = client.get("/email/domains/")
    assert resp.status_code == 200
    assert f'href="/email/domains/{domain.pk}/"'.encode() in resp.content
    # list is a list — no inline DNS-records / verify controls on it
    assert b"add each one at your DNS provider" not in resp.content
    assert f'action="/email/domains/{domain.pk}/verify/"'.encode() not in resp.content


@pytest.mark.django_db
def test_detail_page_renders_management_card(client, account, domain):
    client.force_login(account.owner)
    resp = client.get(f"/email/domains/{domain.pk}/")
    assert resp.status_code == 200
    assert b"mail.acme.com" in resp.content
    assert f'action="/email/domains/{domain.pk}/verify/"'.encode() in resp.content
    assert f'action="/email/domains/{domain.pk}/delete/"'.encode() in resp.content


@pytest.mark.django_db
def test_detail_is_account_scoped(client, domain):
    other_user = User.objects.create_user("intruder", "x@x.com", "pw")
    other_acc = Account.objects.create(company_name="Other")
    Membership.objects.create(
        user=other_user, account=other_acc, role=Membership.Role.OWNER
    )
    client.force_login(other_user)
    resp = client.get(f"/email/domains/{domain.pk}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_create_redirects_to_detail(client, account):
    client.force_login(account.owner)
    resp = client.post("/email/domains/create/", {"domain": "new.acme.com"})
    d = EmailDomain.objects.get(domain="new.acme.com")
    assert resp.status_code == 302
    assert resp["Location"] == f"/email/domains/{d.pk}/"


@pytest.mark.django_db
def test_toggle_redirects_back_to_detail(client, account, domain):
    client.force_login(account.owner)
    resp = client.post(f"/email/domains/{domain.pk}/toggle/")
    assert resp.status_code == 302
    assert resp["Location"] == f"/email/domains/{domain.pk}/"
    domain.refresh_from_db()
    assert domain.is_active is False


@pytest.mark.django_db
def test_delete_redirects_to_list(client, account, domain):
    client.force_login(account.owner)
    resp = client.post(f"/email/domains/{domain.pk}/delete/")
    assert resp.status_code == 302
    assert resp["Location"] == "/email/domains/"
    assert not EmailDomain.objects.filter(pk=domain.pk).exists()

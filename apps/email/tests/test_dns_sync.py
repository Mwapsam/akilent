"""The DNS record spec DomainService keeps in step with the provider.

Covers: MAIL FROM is configured automatically at provisioning and published
as MX + TXT at bounce.<domain>; there is no root SPF row for SES domains; a
resync adds, updates and removes rows to match; an empty DKIM answer from the
provider never deletes the DKIM rows; and a MAIL FROM failure never fails
provisioning.
"""

import pytest
from django.core.cache import cache

from apps.accounts.models import Account
from apps.email.exceptions import EmailProviderError
from apps.email.models import EmailDnsRecord, EmailDomain
from apps.email.types import DkimRecord, DomainInfo, DomainStatus, MailFromInfo

MX = "feedback-smtp.eu-west-1.amazonses.com"


class FakeSes:
    """Just enough of SesProvider for DomainService."""

    def __init__(self, tokens=("tok1", "tok2", "tok3"), mail_from_error=None):
        self.tokens = list(tokens)
        self.mail_from_error = mail_from_error
        self.configure_calls = []

    def create_domain(self, domain, **kwargs):
        return DomainInfo(domain=domain, status=DomainStatus.PENDING, dkim=None)

    def get_dkim_records(self, domain):
        return [
            DkimRecord(
                selector=t,
                algorithm="rsa-sha256",
                public_key_txt=f"{t}.dkim.amazonses.com",
                record_name=f"{t}._domainkey.{domain}",
            )
            for t in self.tokens
        ]

    def _mail_from_mx(self):
        return MX

    def configure_mail_from(self, domain, *, subdomain="bounce"):
        self.configure_calls.append((domain, subdomain))
        if self.mail_from_error:
            raise self.mail_from_error
        return MailFromInfo(
            mail_from_domain=f"{subdomain}.{domain}",
            status="PENDING",
            behavior_on_mx_failure="USE_DEFAULT_VALUE",
            mx_value=MX,
        )


class NoMailFromProvider:
    """A provider with neither MAIL FROM nor multi-record DKIM (Stalwart-like)."""

    def create_domain(self, domain, **kwargs):
        return DomainInfo(
            domain=domain,
            status=DomainStatus.ACTIVE,
            dkim=DkimRecord(
                selector="dkim",
                algorithm="rsa-sha256",
                public_key_txt="v=DKIM1; p=ABC",
                record_name=f"dkim._domainkey.{domain}",
            ),
        )


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.fixture
def domain(account):
    return EmailDomain.objects.create(account=account, domain="mail.acme.com")


def _service(account, provider, monkeypatch):
    from apps.email.services.domain import DomainService

    monkeypatch.setattr(
        "apps.email.services.domain.get_mail_provider", lambda: provider
    )
    return DomainService(account)


def _rows(domain):
    return {(r.key, r.name): r for r in domain.dns_record_rows.all()}


@pytest.mark.django_db
def test_provision_configures_mail_from_and_publishes_its_records(
    account, domain, monkeypatch
):
    provider = FakeSes()
    _service(account, provider, monkeypatch).provision(domain)

    assert provider.configure_calls == [("mail.acme.com", "bounce")]
    domain.refresh_from_db()
    assert domain.mail_from_domain == "bounce.mail.acme.com"
    assert domain.mail_from_status == "PENDING"

    rows = _rows(domain)
    mx = rows[("mfmx", "bounce.mail.acme.com")]
    assert (mx.record_type, mx.value, mx.priority) == ("MX", MX, 10)
    spf = rows[("mfspf", "bounce.mail.acme.com")]
    assert (spf.record_type, spf.value) == ("TXT", "v=spf1 include:amazonses.com ~all")


@pytest.mark.django_db
def test_ses_domains_get_no_root_spf_record(account, domain, monkeypatch):
    """A root SPF row does nothing for SES and can create a second SPF record."""
    _service(account, FakeSes(), monkeypatch).provision(domain)
    assert not domain.dns_record_rows.filter(key="spf").exists()
    keys = [r["key"] for r in domain.dns_records()]
    assert keys == ["verify", "dkim", "dkim", "dkim", "mfmx", "mfspf", "dmarc"]


@pytest.mark.django_db
def test_mail_from_failure_never_fails_provisioning(account, domain, monkeypatch):
    provider = FakeSes(mail_from_error=EmailProviderError("SES throttled"))
    _service(account, provider, monkeypatch).provision(domain)  # must not raise

    domain.refresh_from_db()
    assert domain.mail_from_domain == ""
    assert domain.mail_from_attempted_at is not None  # so refresh can retry later
    # The rest of the spec is still published.
    assert domain.dns_record_rows.filter(key="dkim").count() == 3
    assert not domain.dns_record_rows.filter(key__in=["mfmx", "mfspf"]).exists()


@pytest.mark.django_db
def test_provider_without_the_capability_is_a_no_op(account, domain, monkeypatch):
    _service(account, NoMailFromProvider(), monkeypatch).provision(domain)
    domain.refresh_from_db()
    assert domain.mail_from_domain == ""
    assert not domain.dns_record_rows.exists()  # legacy single-TXT path


@pytest.mark.django_db
def test_resync_removes_stale_root_spf_and_rotated_dkim(account, domain, monkeypatch):
    # State from before this change: a root SPF row and a DKIM token SES dropped.
    EmailDnsRecord.objects.create(
        domain=domain,
        key="spf",
        record_type="TXT",
        name="mail.acme.com",
        value="v=spf1 include:amazonses.com ~all",
    )
    EmailDnsRecord.objects.create(
        domain=domain,
        key="dkim",
        record_type="CNAME",
        name="old._domainkey.mail.acme.com",
        value="old.dkim.amazonses.com",
    )
    domain.mail_from_domain = "bounce.mail.acme.com"
    domain.save(update_fields=["mail_from_domain"])

    diff = _service(account, FakeSes(), monkeypatch).sync_dns_records(domain)

    rows = _rows(domain)
    assert ("spf", "mail.acme.com") not in rows
    assert ("dkim", "old._domainkey.mail.acme.com") not in rows
    assert {r.name for r in diff["remove"]} == {
        "mail.acme.com",
        "old._domainkey.mail.acme.com",
    }
    assert ("mfmx", "bounce.mail.acme.com") in rows


@pytest.mark.django_db
def test_empty_dkim_answer_never_deletes_dkim_rows(account, domain, monkeypatch):
    """A provider hiccup returning no tokens must not wipe the DKIM spec."""
    svc = _service(account, FakeSes(), monkeypatch)
    svc.sync_dns_records(domain)
    assert domain.dns_record_rows.filter(key="dkim").count() == 3

    svc._provider.tokens = []
    diff = svc.sync_dns_records(domain)

    assert domain.dns_record_rows.filter(key="dkim").count() == 3
    assert not [r for r in diff["remove"] if r.key == "dkim"]


@pytest.mark.django_db
def test_changed_value_is_updated_and_reset_to_unchecked(account, domain, monkeypatch):
    domain.mail_from_domain = "bounce.mail.acme.com"
    domain.save(update_fields=["mail_from_domain"])
    EmailDnsRecord.objects.create(
        domain=domain,
        key="mfmx",
        record_type="MX",
        name="bounce.mail.acme.com",
        value="feedback-smtp.us-east-1.amazonses.com",
        priority=10,
        is_ok=True,
    )

    _service(account, FakeSes(), monkeypatch).sync_dns_records(domain)

    row = domain.dns_record_rows.get(key="mfmx")
    assert row.value == MX
    assert row.is_ok is False  # must be re-checked against the new value


@pytest.mark.django_db
def test_ensure_dns_spec_upgrades_an_existing_domain_once(account, domain, monkeypatch):
    """Domains provisioned before MAIL FROM existed are brought up to spec."""
    provider = FakeSes()
    svc = _service(account, provider, monkeypatch)

    assert svc.ensure_dns_spec(domain) is True
    domain.refresh_from_db()
    assert domain.mail_from_domain == "bounce.mail.acme.com"
    assert domain.dns_record_rows.filter(key="mfmx").exists()

    # Now complete: nothing further to do.
    assert svc.ensure_dns_spec(domain) is False


@pytest.mark.django_db
def test_ensure_dns_spec_is_rate_limited(account, domain, monkeypatch):
    """The 20-second card poll must not call the provider on every tick."""
    provider = FakeSes(mail_from_error=EmailProviderError("down"))
    svc = _service(account, provider, monkeypatch)

    svc.ensure_dns_spec(domain)
    svc.ensure_dns_spec(domain)
    assert len(provider.configure_calls) == 1


@pytest.mark.django_db
def test_dry_run_plan_writes_nothing(account, domain, monkeypatch):
    svc = _service(account, FakeSes(), monkeypatch)
    desired, diff = svc.planned_dns_records(
        domain, mail_from_domain="bounce.mail.acme.com"
    )
    assert {d.key for d in diff["add"]} >= {"mfmx", "mfspf"}
    assert not domain.dns_record_rows.exists()
    domain.refresh_from_db()
    assert domain.mail_from_domain == ""

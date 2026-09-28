"""Tests for apps.email.verification.refresh_domain and the reverify task."""

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.email import dnscheck
from apps.email.models import EmailDomain
from apps.email.tasks import reverify_pending_domains
from apps.email.verification import refresh_domain

VERIFY_VALUE = "automator-domain-verification=abcd1234"


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
        dkim_public_key="v=DKIM1; k=rsa; p=ABCDEF",
        verify_record_name="mail.acme.com",
        verify_record_value=VERIFY_VALUE,
    )


def _rows(result_by_key):
    def _inner(record):
        return [
            {**row, "ok": result_by_key.get(row["key"], False)}
            for row in record.dns_records()
        ]

    return _inner


@pytest.mark.django_db
def test_refresh_domain_transitions_pending_to_verified(domain, monkeypatch):
    monkeypatch.setattr(
        dnscheck,
        "check_records",
        _rows({"verify": True, "dkim": True, "spf": True, "dmarc": True}),
    )
    out = refresh_domain(domain)
    assert out == {"verify": True, "dkim": True, "spf": True, "dmarc": True}
    domain.refresh_from_db()
    assert domain.is_verified
    assert domain.verified_at is not None


@pytest.mark.django_db
def test_refresh_domain_stays_pending_without_ownership(domain, monkeypatch):
    monkeypatch.setattr(
        dnscheck,
        "check_records",
        _rows({"verify": False, "dkim": True, "spf": False, "dmarc": False}),
    )
    refresh_domain(domain)
    domain.refresh_from_db()
    assert not domain.is_verified
    assert domain.dkim_ok is True


@pytest.mark.django_db
def test_refresh_domain_ses_needs_provider_success(domain, monkeypatch):
    monkeypatch.setattr(EmailDomain, "is_ses_backed", lambda self: True)
    monkeypatch.setattr(
        dnscheck,
        "check_records",
        _rows({"verify": True, "dkim": True, "spf": True, "dmarc": True}),
    )

    # DNS is all green but SES itself hasn't flipped to SUCCESS yet.
    monkeypatch.setattr(
        "apps.email.verification._ses_identity_verified", lambda rec, prov: False
    )
    refresh_domain(domain)
    domain.refresh_from_db()
    assert not domain.is_verified

    # SES now reports SUCCESS -> verified.
    monkeypatch.setattr(
        "apps.email.verification._ses_identity_verified", lambda rec, prov: True
    )
    refresh_domain(domain)
    domain.refresh_from_db()
    assert domain.is_verified


@pytest.mark.django_db
def test_reverify_pending_domains_only_touches_pending(account, monkeypatch):
    EmailDomain.objects.create(
        account=account,
        domain="a.acme.com",
        verify_record_name="a.acme.com",
        verify_record_value=VERIFY_VALUE,
    )
    EmailDomain.objects.create(
        account=account,
        domain="b.acme.com",
        status=EmailDomain.Status.VERIFIED,
        verify_record_name="b.acme.com",
        verify_record_value=VERIFY_VALUE,
    )

    seen = []

    def _fake_refresh(record, **kw):
        seen.append(record.domain)
        record.status = EmailDomain.Status.VERIFIED
        record.save(update_fields=["status"])
        return {}

    monkeypatch.setattr("apps.email.verification.refresh_domain", _fake_refresh)

    verified = reverify_pending_domains()
    assert seen == ["a.acme.com"]
    assert verified == 1


# --- MAIL FROM, rollups, drift ------------------------------------------------


@pytest.fixture(autouse=False)
def clear_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def ses_mail_from_domain(account, clear_cache):
    from apps.email.models import EmailDnsRecord

    d = EmailDomain.objects.create(
        account=account,
        domain="mail.acme.com",
        verify_record_name="mail.acme.com",
        verify_record_value=VERIFY_VALUE,
        mail_from_domain="bounce.mail.acme.com",
        mail_from_status="FAILED",
    )
    EmailDnsRecord.objects.create(
        domain=d,
        key="verify",
        record_type="TXT",
        name="mail.acme.com",
        value=VERIFY_VALUE,
    )
    EmailDnsRecord.objects.create(
        domain=d,
        key="mfmx",
        record_type="MX",
        name="bounce.mail.acme.com",
        value="feedback-smtp.us-east-1.amazonses.com",
        priority=10,
    )
    EmailDnsRecord.objects.create(
        domain=d,
        key="mfspf",
        record_type="TXT",
        name="bounce.mail.acme.com",
        value="v=spf1 include:amazonses.com ~all",
    )
    return d


class _SesStub:
    """Provider stub reporting a given MAIL FROM status and recording retries."""

    def __init__(self, status):
        from apps.email.types import MailFromInfo

        self.info = MailFromInfo("bounce.mail.acme.com", status=status)
        self.configure_calls = 0
        self.verify_calls = 0

    def get_mail_from(self, domain):
        return self.info

    def configure_mail_from(self, domain, *, subdomain="bounce"):
        self.configure_calls += 1
        return self.info

    def verify_domain(self, domain):
        from apps.email.types import OperationResult

        self.verify_calls += 1
        return OperationResult(success=True)


@pytest.mark.django_db
def test_failed_mail_from_is_retriggered_once_records_are_found(
    ses_mail_from_domain, monkeypatch
):
    """SES stops re-checking a FAILED MAIL FROM; we ask again, at most daily."""
    from django.core.cache import cache

    monkeypatch.setattr(dnscheck, "check_records", _rows({"mfmx": True, "mfspf": True}))
    provider = _SesStub("FAILED")

    refresh_domain(ses_mail_from_domain, provider=provider)
    assert provider.configure_calls == 1
    ses_mail_from_domain.refresh_from_db()
    assert ses_mail_from_domain.mail_from_ok is True

    # Same day, even after the status cache expires: no second retry.
    cache.clear()
    refresh_domain(ses_mail_from_domain, provider=provider)
    assert provider.configure_calls == 1


@pytest.mark.django_db
def test_failed_mail_from_is_not_retriggered_while_records_are_missing(
    ses_mail_from_domain, monkeypatch
):
    monkeypatch.setattr(
        dnscheck, "check_records", _rows({"mfmx": False, "mfspf": True})
    )
    provider = _SesStub("FAILED")
    refresh_domain(ses_mail_from_domain, provider=provider)
    assert provider.configure_calls == 0


@pytest.mark.django_db
def test_mail_from_status_is_mirrored_and_never_gates_verification(
    ses_mail_from_domain, monkeypatch
):
    """MAIL FROM missing: still verified via ownership; status reported, not gating."""
    monkeypatch.setattr(dnscheck, "check_records", _rows({"verify": True}))
    provider = _SesStub("PENDING")

    refresh_domain(ses_mail_from_domain, provider=provider)
    ses_mail_from_domain.refresh_from_db()
    assert ses_mail_from_domain.mail_from_status == "PENDING"
    assert ses_mail_from_domain.mail_from_ok is False
    assert ses_mail_from_domain.is_verified  # ownership alone decides (non-SES here)


@pytest.mark.django_db
def test_spf_ok_follows_the_mail_from_spf_for_ses(ses_mail_from_domain, monkeypatch):
    monkeypatch.setattr(dnscheck, "check_records", _rows({"mfspf": True}))
    refresh_domain(ses_mail_from_domain, provider=_SesStub("PENDING"))
    ses_mail_from_domain.refresh_from_db()
    assert ses_mail_from_domain.spf_ok is True


@pytest.mark.django_db
def test_verified_domain_skips_the_ses_identity_call(domain, monkeypatch):
    domain.status = EmailDomain.Status.VERIFIED
    domain.save(update_fields=["status"])
    monkeypatch.setattr(EmailDomain, "is_ses_backed", lambda self: True)
    monkeypatch.setattr(dnscheck, "check_records", _rows({}))  # everything missing
    provider = _SesStub("SUCCESS")

    refresh_domain(domain, provider=provider)

    assert provider.verify_calls == 0
    domain.refresh_from_db()
    assert domain.is_verified  # never downgraded by a failing check


@pytest.mark.django_db
def test_failing_records_are_persisted_with_their_reason(domain, monkeypatch):
    """The card reads diagnostics from the row, so it never does DNS itself."""
    monkeypatch.setattr(dnscheck, "_resolve_txt", lambda n: [])
    refresh_domain(domain)
    domain.refresh_from_db()
    assert domain.dns_diagnostics["verify|mail.acme.com"]["code"] == "missing"


@pytest.mark.django_db
def test_recheck_verified_domains_only_touches_stale_verified(account, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from apps.email.tasks import recheck_verified_domains

    now = timezone.now()
    stale = EmailDomain.objects.create(
        account=account,
        domain="stale.acme.com",
        status=EmailDomain.Status.VERIFIED,
        last_checked_at=now - timedelta(hours=30),
    )
    EmailDomain.objects.create(
        account=account,
        domain="fresh.acme.com",
        status=EmailDomain.Status.VERIFIED,
        last_checked_at=now - timedelta(hours=1),
    )
    EmailDomain.objects.create(
        account=account,
        domain="pending.acme.com",
        status=EmailDomain.Status.PENDING,
    )
    seen = []
    monkeypatch.setattr(
        "apps.email.verification.refresh_domain", lambda d, **k: seen.append(d.domain)
    )

    assert recheck_verified_domains() == 1
    assert seen == [stale.domain]

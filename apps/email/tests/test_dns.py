import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.email import dnscheck
from apps.email.models import EmailDomain

DKIM_PUBLIC = "v=DKIM1; k=rsa; p=ABCDEFGHIJKLMNOP"
VERIFY_VALUE = "automator-domain-verification=deadbeefcafe1234"


@pytest.fixture
def account(db):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    acc = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    return acc


@pytest.fixture
def domain(account):
    return EmailDomain.objects.create(
        account=account, domain="mail.acme.com",
        dkim_public_key=DKIM_PUBLIC,
        verify_record_name="mail.acme.com", verify_record_value=VERIFY_VALUE,
    )


def _fake_resolver(mapping):
    def _resolve(name):
        return mapping.get(name, [])
    return _resolve


def _fake_check_records(result_by_key):
    """Stand in for dnscheck.check_records: reuse the real dns_records() spec
    but force each row's ``ok`` from a {key: bool} map."""
    def _inner(record):
        return [
            {**row, "ok": result_by_key.get(row["key"], False)}
            for row in record.dns_records()
        ]
    return _inner


# --- Model -------------------------------------------------------------------

def test_ensure_verification_token_is_idempotent():
    d = EmailDomain(domain="x.com")
    assert d.ensure_verification_token() is True
    assert d.verify_record_value.startswith("automator-domain-verification=")
    first = d.verify_record_value
    # A second call doesn't churn the token.
    assert d.ensure_verification_token() is False
    assert d.verify_record_value == first


@pytest.mark.django_db
def test_dns_records_spec_and_counts(domain):
    keys = [r["key"] for r in domain.dns_records()]
    assert keys == ["verify", "dkim", "spf", "dmarc"]
    assert domain.dns_total_count == 4
    assert domain.dns_found_count == 0  # nothing verified yet


# --- Checker -----------------------------------------------------------------

@pytest.mark.django_db
def test_check_domain_all_present(domain, monkeypatch, settings):
    settings.EMAIL_HOST = "smtp.relay.com"
    monkeypatch.setattr(dnscheck, "_resolve_txt", _fake_resolver({
        "mail.acme.com": [VERIFY_VALUE, "v=spf1 include:smtp.relay.com ~all"],
        "dkim._domainkey.mail.acme.com": [DKIM_PUBLIC],
        "_dmarc.mail.acme.com": ["v=DMARC1; p=none"],
    }))
    res = dnscheck.check_domain(domain)
    assert res == {"verify": True, "dkim": True, "spf": True, "dmarc": True}


@pytest.mark.django_db
def test_check_domain_missing_records(domain, monkeypatch, settings):
    settings.EMAIL_HOST = "smtp.relay.com"
    # Only the verification TXT is published; everything else absent.
    monkeypatch.setattr(dnscheck, "_resolve_txt", _fake_resolver({
        "mail.acme.com": [VERIFY_VALUE],
    }))
    res = dnscheck.check_domain(domain)
    assert res["verify"] is True
    assert res["dkim"] is False
    assert res["spf"] is False
    assert res["dmarc"] is False


@pytest.mark.django_db
def test_spf_requires_matching_include(domain, monkeypatch, settings):
    settings.EMAIL_HOST = "smtp.relay.com"
    # An SPF record for a *different* host shouldn't count.
    monkeypatch.setattr(dnscheck, "_resolve_txt", _fake_resolver({
        "mail.acme.com": ["v=spf1 include:someone-else.com ~all"],
    }))
    assert dnscheck.check_domain(domain)["spf"] is False


def test_value_comparison_is_whitespace_insensitive():
    # Providers may wrap/space long TXT values; matching must tolerate it.
    assert dnscheck._contains.__module__  # sanity: function exists
    norm = dnscheck._norm
    assert norm("v=DKIM1;  p=AB CD") == "v=dkim1;p=abcd"


# --- Verify view -------------------------------------------------------------

@pytest.mark.django_db
def test_verify_view_marks_domain_verified(client, account, domain, monkeypatch):
    client.force_login(account.owner)
    monkeypatch.setattr(
        "apps.email.dnscheck.check_records",
        _fake_check_records({"verify": True, "dkim": True, "spf": True, "dmarc": True}),
    )
    resp = client.post(f"/email/domains/{domain.pk}/verify/")
    assert resp.status_code == 302
    domain.refresh_from_db()
    assert domain.is_verified
    assert domain.dkim_ok and domain.spf_ok and domain.dmarc_ok
    assert domain.last_checked_at is not None


@pytest.mark.django_db
def test_verify_view_stays_pending_without_ownership(client, account, domain, monkeypatch):
    client.force_login(account.owner)
    monkeypatch.setattr(
        "apps.email.dnscheck.check_records",
        _fake_check_records({"verify": False, "dkim": True, "spf": False, "dmarc": False}),
    )
    resp = client.post(f"/email/domains/{domain.pk}/verify/")
    assert resp.status_code == 302
    domain.refresh_from_db()
    assert not domain.is_verified
    assert domain.dkim_ok is True  # flags still refresh while pending


# --- SES Easy DKIM (3 CNAMEs) ----------------------------------------------

@pytest.fixture
def ses_domain(account, monkeypatch):
    """A domain whose DNS spec is EmailDnsRecord rows, as DomainService creates
    for SES: verify TXT, 3 DKIM CNAMEs, the MAIL FROM MX + SPF TXT at
    bounce.<domain>, DMARC TXT. No root SPF record."""
    from apps.email.models import EmailDnsRecord

    d = EmailDomain.objects.create(
        account=account, domain="mail.acme.com",
        verify_record_name="mail.acme.com", verify_record_value=VERIFY_VALUE,
        mail_from_domain="bounce.mail.acme.com",
    )
    EmailDnsRecord.objects.create(
        domain=d, key="verify", record_type="TXT",
        name="mail.acme.com", value=VERIFY_VALUE,
    )
    for tok in ("tok1", "tok2", "tok3"):
        EmailDnsRecord.objects.create(
            domain=d, key="dkim", record_type="CNAME",
            name=f"{tok}._domainkey.mail.acme.com",
            value=f"{tok}.dkim.amazonses.com",
        )
    EmailDnsRecord.objects.create(
        domain=d, key="mfmx", record_type="MX", name="bounce.mail.acme.com",
        value="feedback-smtp.us-east-1.amazonses.com", priority=10,
    )
    EmailDnsRecord.objects.create(
        domain=d, key="mfspf", record_type="TXT",
        name="bounce.mail.acme.com", value="v=spf1 include:amazonses.com ~all",
    )
    EmailDnsRecord.objects.create(
        domain=d, key="dmarc", record_type="TXT",
        name="_dmarc.mail.acme.com", value="v=DMARC1; p=none",
    )
    return d


@pytest.mark.django_db
def test_ses_dns_records_are_cname_backed(ses_domain):
    recs = ses_domain.dns_records()
    keys = [r["key"] for r in recs]
    assert keys == ["verify", "dkim", "dkim", "dkim", "mfmx", "mfspf", "dmarc"]
    dkim_rows = [r for r in recs if r["key"] == "dkim"]
    assert all(r["type"] == "CNAME" for r in dkim_rows)
    mx = next(r for r in recs if r["key"] == "mfmx")
    assert (mx["type"], mx["priority"]) == ("MX", 10)
    # What to type into a DNS host that appends the zone for you.
    assert mx["host"] == "bounce.mail"
    assert mx["zone"] == "acme.com"
    assert ses_domain.dns_total_count == 7


@pytest.mark.django_db
def test_ses_check_domain_needs_all_three_cnames(ses_domain, monkeypatch, settings):
    settings.EMAIL_HOST = ""  # SES deployments have no SMTP host
    monkeypatch.setattr(dnscheck, "_resolve_txt", _fake_resolver({
        "mail.acme.com": [VERIFY_VALUE],
        "bounce.mail.acme.com": ["v=spf1 include:amazonses.com ~all"],
        "_dmarc.mail.acme.com": ["v=DMARC1; p=none"],
    }))
    monkeypatch.setattr(dnscheck, "_resolve_mx", _fake_resolver({
        "bounce.mail.acme.com": [(10, "feedback-smtp.us-east-1.amazonses.com.")],
    }))
    # Only two of the three DKIM CNAMEs resolve.
    monkeypatch.setattr(dnscheck, "_resolve_cname", _fake_resolver({
        "tok1._domainkey.mail.acme.com": ["tok1.dkim.amazonses.com."],
        "tok2._domainkey.mail.acme.com": ["tok2.dkim.amazonses.com."],
    }))
    res = dnscheck.check_domain(ses_domain)
    assert res == {"verify": True, "dkim": False, "mfmx": True, "mfspf": True, "dmarc": True}

    # Publish the third — DKIM (and the whole spec) goes green.
    monkeypatch.setattr(dnscheck, "_resolve_cname", _fake_resolver({
        "tok1._domainkey.mail.acme.com": ["tok1.dkim.amazonses.com."],
        "tok2._domainkey.mail.acme.com": ["tok2.dkim.amazonses.com."],
        "tok3._domainkey.mail.acme.com": ["tok3.dkim.amazonses.com."],
    }))
    res = dnscheck.check_domain(ses_domain)
    assert res == {"verify": True, "dkim": True, "mfmx": True, "mfspf": True, "dmarc": True}


@pytest.mark.django_db
def test_ses_spf_check_ignores_email_host(ses_domain, monkeypatch, settings):
    # Regression: the old checker compared SPF against settings.EMAIL_HOST, which
    # is empty on SES, so spf_ok could never go true. It must follow the record's
    # own value (include:amazonses.com) instead.
    settings.EMAIL_HOST = ""
    monkeypatch.setattr(dnscheck, "_resolve_txt", _fake_resolver({
        "bounce.mail.acme.com": ["v=spf1 include:amazonses.com ~all"],
    }))
    monkeypatch.setattr(dnscheck, "_resolve_cname", _fake_resolver({}))
    # SES SPF is checked where SES evaluates it: the MAIL FROM subdomain.
    assert dnscheck.check_domain(ses_domain)["mfspf"] is True


@pytest.mark.django_db
def test_refresh_domain_persists_per_cname_readiness(ses_domain, monkeypatch, settings):
    from apps.email.verification import refresh_domain

    settings.EMAIL_HOST = ""
    monkeypatch.setattr(dnscheck, "_resolve_txt", _fake_resolver({
        "mail.acme.com": [VERIFY_VALUE],
        "bounce.mail.acme.com": ["v=spf1 include:amazonses.com ~all"],
        "_dmarc.mail.acme.com": ["v=DMARC1; p=none"],
    }))
    monkeypatch.setattr(dnscheck, "_resolve_cname", _fake_resolver({
        "tok1._domainkey.mail.acme.com": ["tok1.dkim.amazonses.com."],
    }))
    # SES itself not yet reporting SUCCESS -> stays pending even though 1 CNAME is up.
    monkeypatch.setattr(
        "apps.email.verification._ses_identity_verified", lambda rec, prov: False
    )
    monkeypatch.setattr(EmailDomain, "is_ses_backed", lambda self: True)

    refresh_domain(ses_domain)
    rows = {r.name: r.is_ok for r in ses_domain.dns_record_rows.all()}
    assert rows["tok1._domainkey.mail.acme.com"] is True
    assert rows["tok2._domainkey.mail.acme.com"] is False
    ses_domain.refresh_from_db()
    assert not ses_domain.is_verified  # DKIM incomplete + SES not SUCCESS
    # The reason is persisted, so the card can say it without doing DNS.
    diag = ses_domain.dns_diagnostics["dkim|tok2._domainkey.mail.acme.com"]
    assert diag["code"] == "missing"
    # MAIL FROM MX isn't published: recommended, so it doesn't block anything.
    assert ses_domain.dns_diagnostics["mfmx|bounce.mail.acme.com"]["code"] == "missing"
    assert ses_domain.mail_from_ok is False


@pytest.mark.django_db
def test_create_view_mints_verification_token(client, account, monkeypatch):
    client.force_login(account.owner)

    from apps.email.types import DkimRecord, DomainInfo, DomainStatus

    class FakeProvider:
        def create_domain(self, domain, **kwargs):
            return DomainInfo(
                domain=domain,
                status=DomainStatus.ACTIVE,
                dkim=DkimRecord(
                    selector="dkim",
                    algorithm="rsa-sha256",
                    public_key_txt=DKIM_PUBLIC,
                    record_name=f"dkim._domainkey.{domain}",
                ),
            )

    monkeypatch.setattr(
        "apps.email.services.domain.get_mail_provider", lambda: FakeProvider()
    )
    resp = client.post("/email/domains/create/", {"domain": "new.acme.com"})
    assert resp.status_code == 302
    d = EmailDomain.objects.get(domain="new.acme.com")
    assert d.verify_record_value.startswith("automator-domain-verification=")
    assert d.dkim_public_key == DKIM_PUBLIC

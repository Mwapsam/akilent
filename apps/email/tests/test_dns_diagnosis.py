"""Per-record DNS diagnosis: every failure says *why*, provider-neutrally.

The core invariant: a lookup that timed out or failed is never reported as
"missing" -- that would tell a tenant to add a record they already added.
"""

import pytest

from apps.email import dnscheck, dnshost
from apps.email.dnscheck import NOANSWER, NXDOMAIN, SERVFAIL, TIMEOUT, Answer

ZONE, DOMAIN = "acme.com", "mail.acme.com"
MX = "feedback-smtp.us-east-1.amazonses.com"


def _txt(**by_name):
    return lambda name: by_name.get(name, [])


def _patch(monkeypatch, *, txt=None, cname=None, mx=None, a=None):
    monkeypatch.setattr(dnscheck, "_resolve_txt", lambda n: (txt or {}).get(n, []))
    monkeypatch.setattr(dnscheck, "_resolve_cname", lambda n: (cname or {}).get(n, []))
    monkeypatch.setattr(dnscheck, "_resolve_mx", lambda n: (mx or {}).get(n, []))
    monkeypatch.setattr(dnscheck, "_resolve_a", lambda n: (a or {}).get(n, []))


def _row(key, rtype, name, value, priority=None):
    return {
        "key": key,
        "type": rtype,
        "name": name,
        "value": value,
        "priority": priority,
    }


MX_ROW = _row("mfmx", "MX", "bounce.mail.acme.com", MX, 10)
SPF_ROW = _row(
    "mfspf", "TXT", "bounce.mail.acme.com", "v=spf1 include:amazonses.com ~all"
)
DKIM_ROW = _row(
    "dkim", "CNAME", "tok1._domainkey.mail.acme.com", "tok1.dkim.amazonses.com"
)
VERIFY_ROW = _row(
    "verify", "TXT", "mail.acme.com", "automator-domain-verification=abc123"
)


def _diag(row):
    return dnscheck.diagnose(row, ZONE, DOMAIN)


# --- Found / missing ---------------------------------------------------------


def test_found_records_are_ok(monkeypatch):
    _patch(
        monkeypatch,
        txt={
            "bounce.mail.acme.com": ["v=spf1 include:amazonses.com ~all"],
            "mail.acme.com": ["automator-domain-verification=abc123"],
        },
        cname={"tok1._domainkey.mail.acme.com": ["tok1.dkim.amazonses.com."]},
        mx={"bounce.mail.acme.com": [(10, MX + ".")]},
    )
    for row in (MX_ROW, SPF_ROW, DKIM_ROW, VERIFY_ROW):
        d = _diag(row)
        assert (d.ok, d.code) == (True, "ok"), row["key"]


def test_absent_record_is_not_added_yet(monkeypatch):
    _patch(monkeypatch)
    d = _diag(MX_ROW)
    assert (d.ok, d.code) == (False, "missing")
    assert d.message == "Not added yet."


# --- Timeout is never "missing" ----------------------------------------------


@pytest.mark.parametrize("status", [TIMEOUT, SERVFAIL])
@pytest.mark.parametrize("row", [MX_ROW, SPF_ROW, DKIM_ROW, VERIFY_ROW])
def test_failed_lookup_is_never_reported_missing(monkeypatch, status, row):
    failed = lambda n: Answer((), status)  # noqa: E731
    monkeypatch.setattr(dnscheck, "_resolve_txt", failed)
    monkeypatch.setattr(dnscheck, "_resolve_cname", failed)
    monkeypatch.setattr(dnscheck, "_resolve_mx", failed)
    monkeypatch.setattr(dnscheck, "_resolve_a", failed)
    d = _diag(row)
    assert d.ok is False
    assert d.code == "timeout"
    assert d.code != "missing"
    assert "didn't answer" in d.message


def test_nxdomain_and_noanswer_both_mean_missing(monkeypatch):
    for status in (NXDOMAIN, NOANSWER):
        monkeypatch.setattr(dnscheck, "_resolve_mx", lambda n, s=status: Answer((), s))
        monkeypatch.setattr(dnscheck, "_resolve_cname", lambda n: [])
        monkeypatch.setattr(dnscheck, "_resolve_txt", lambda n: [])
        assert _diag(MX_ROW).code == "missing"


# --- Wrong name --------------------------------------------------------------


def test_doubled_zone_suffix_is_explained_with_the_host_to_type(monkeypatch):
    """Typing the full name into a host that appends the zone doubles it."""
    _patch(monkeypatch, mx={"bounce.mail.acme.com.acme.com": [(10, MX + ".")]})
    d = _diag(MX_ROW)
    assert d.code == "doubled"
    assert "bounce.mail.acme.com.acme.com" in d.message
    assert '"bounce.mail"' in d.message  # exactly what to type instead


def test_record_added_without_the_sending_subdomain(monkeypatch):
    """mail.acme.com is in zone acme.com; bounce.acme.com is the wrong place."""
    _patch(monkeypatch, txt={"bounce.acme.com": ["v=spf1 include:amazonses.com ~all"]})
    d = _diag(SPF_ROW)
    assert d.code == "wrong_name"
    assert "bounce.acme.com" in d.message and "bounce.mail.acme.com" in d.message


# --- Wrong value / type -------------------------------------------------------


def test_two_spf_records_is_an_error_with_a_merged_suggestion(monkeypatch):
    _patch(
        monkeypatch,
        txt={
            "bounce.mail.acme.com": [
                "v=spf1 include:amazonses.com ~all",
                "v=spf1 include:_spf.mx.cloudflare.net ~all",
            ]
        },
    )
    d = _diag(SPF_ROW)
    assert (d.ok, d.code) == (False, "multiple_spf")
    assert len(d.found) == 2
    assert d.suggestion == (
        "v=spf1 include:amazonses.com include:_spf.mx.cloudflare.net ~all"
    )


def test_existing_spf_missing_our_include(monkeypatch):
    _patch(monkeypatch, txt={"bounce.mail.acme.com": ["v=spf1 include:other.net ~all"]})
    d = _diag(SPF_ROW)
    assert d.code == "wrong_value"
    assert "include:amazonses.com" in d.message
    assert (
        "include:amazonses.com" in d.suggestion and "include:other.net" in d.suggestion
    )


def test_wrong_cname_target_shows_what_was_found(monkeypatch):
    _patch(monkeypatch, cname={"tok1._domainkey.mail.acme.com": ["wrong.example.com."]})
    d = _diag(DKIM_ROW)
    assert d.code == "wrong_value"
    assert d.found == ["wrong.example.com."]


def test_txt_where_cname_is_required(monkeypatch):
    _patch(
        monkeypatch, txt={"tok1._domainkey.mail.acme.com": ["tok1.dkim.amazonses.com"]}
    )
    assert _diag(DKIM_ROW).code == "wrong_type"


def test_proxied_or_flattened_cname(monkeypatch):
    _patch(monkeypatch, a={"tok1._domainkey.mail.acme.com": ["104.16.0.1"]})
    d = _diag(DKIM_ROW)
    assert d.code == "proxied"
    # Provider-neutral wording: no specific DNS host is named.
    assert "cloudflare" not in d.message.lower()


def test_mx_priority_typed_into_the_server_field(monkeypatch):
    _patch(monkeypatch, mx={"bounce.mail.acme.com": [(0, "10." + MX + ".")]})
    d = _diag(MX_ROW)
    assert d.code == "wrong_value"
    assert "Priority" in d.message


def test_mx_pointing_elsewhere(monkeypatch):
    _patch(monkeypatch, mx={"bounce.mail.acme.com": [(10, "mx.other.net.")]})
    d = _diag(MX_ROW)
    assert d.code == "mx_mismatch"
    assert d.found == ["10 mx.other.net."]


def test_any_mx_priority_is_accepted(monkeypatch):
    _patch(monkeypatch, mx={"bounce.mail.acme.com": [(20, MX + ".")]})
    assert _diag(MX_ROW).ok is True


def test_cname_at_the_bounce_name_blocks_mx_and_txt(monkeypatch):
    _patch(monkeypatch, cname={"bounce.mail.acme.com": ["something.else.com."]})
    assert _diag(MX_ROW).code == "cname_conflict"
    assert _diag(SPF_ROW).code == "cname_conflict"


def test_stale_verification_value(monkeypatch):
    _patch(monkeypatch, txt={"mail.acme.com": ["automator-domain-verification=OLD"]})
    d = _diag(VERIFY_ROW)
    assert d.code == "wrong_value"
    assert d.found == ["automator-domain-verification=OLD"]


# --- Root SPF advice ---------------------------------------------------------


def test_root_spf_advice_only_when_there_are_two(monkeypatch):
    _patch(
        monkeypatch,
        txt={"mail.acme.com": ["v=spf1 include:_spf.mx.cloudflare.net ~all"]},
    )
    assert dnscheck.check_root_spf("mail.acme.com") is None

    _patch(
        monkeypatch,
        txt={
            "mail.acme.com": [
                "v=spf1 include:_spf.mx.cloudflare.net ~all",
                "v=spf1 include:amazonses.com ~all",
            ]
        },
    )
    advice = dnscheck.check_root_spf("mail.acme.com")
    assert advice["code"] == "multiple_spf"
    assert "delete it" in advice["message"]


# --- dnshost (pure) ----------------------------------------------------------


@pytest.mark.parametrize(
    "domain,zone",
    [
        ("mail.acme.com", "acme.com"),
        ("acme.com", "acme.com"),
        ("mail.acme.co.zm", "acme.co.zm"),
        ("mail.acme.co.uk", "acme.co.uk"),
        ("news.shop.acme.co.uk", "acme.co.uk"),
    ],
)
def test_guess_zone(domain, zone):
    assert dnshost.guess_zone(domain) == zone


@pytest.mark.parametrize(
    "name,zone,host",
    [
        ("bounce.mail.acme.com", "acme.com", "bounce.mail"),
        ("acme.com", "acme.com", "@"),
        ("_dmarc.acme.com.", "acme.com", "_dmarc"),
        ("x.other.org", "acme.com", "x.other.org"),
        ("bounce.other.com", "acme.com", "bounce.other.com"),
        ("", "acme.com", "@"),
        ("bounce.acme.com", "", "bounce.acme.com"),
    ],
)
def test_relative_host(name, zone, host):
    assert dnshost.relative_host(name, zone) == host


@pytest.mark.parametrize(
    "ns,slug",
    [
        (["ada.ns.cloudflare.com"], "cloudflare"),
        (["ns01.domaincontrol.com"], "godaddy"),
        (["dns1.registrar-servers.com"], "namecheap"),
        # Real Route 53 nameservers: the distinctive part is the awsdns-<nn> label.
        (["ns-1234.awsdns-12.co.uk"], "route53"),
        (["ns-123.awsdns-45.org."], "route53"),
        (["ns-99.awsdns-01.com"], "route53"),
        # Cloudflare customer NS are <name>.ns.cloudflare.com; label-boundary
        # suffix matching also covers the plain form, but not look-alikes.
        (["ns1.cloudflare.com"], "cloudflare"),
        (["notcloudflare.com"], "other"),
        (["ns1.awsdns-imitation.example"], "other"),
        (["unknown.example"], "other"),
        (["ns-cloud-a1.googledomains.com"], "google"),
        (["ns1.digitalocean.com"], "digitalocean"),
        (["ns1-01.azure-dns.com"], "azure"),
        (["ns1.dns-parking.com"], "hostinger"),
        (["ns1.some-local-isp.co.zm"], "other"),
        ([], "other"),
    ],
)
def test_detect_host(ns, slug):
    assert dnshost.detect_host(ns) == slug


def test_tabs_put_the_detected_host_first_and_never_default_to_one():
    assert dnshost.ordered_tabs("godaddy")[0][0] == "godaddy"
    # Unknown host: generic steps first, not any particular provider.
    assert dnshost.ordered_tabs("")[0][0] == "other"
    assert dnshost.ordered_tabs("other")[0][0] == "other"
    assert {s for s, _ in dnshost.ordered_tabs("")} >= {"cloudflare", "other"}


# --- Authoritative lookups fall back safely ----------------------------------


class _Resolver:
    def __init__(self, exc=None, value=None):
        self.exc, self.value = exc, value

    def resolve(self, name, rdtype, lifetime=None):
        if self.exc:
            raise self.exc
        return self.value


@pytest.fixture
def auth_resolver():
    """Install a fake authoritative resolver for the duration of a test."""
    tokens = []

    def _install(resolver):
        tokens.append(dnscheck._AUTH.set(resolver))

    yield _install
    for token in reversed(tokens):
        dnscheck._AUTH.reset(token)


def test_authoritative_timeout_falls_back_to_system_resolver(
    real_dns_query, monkeypatch, auth_resolver
):
    import dns.resolver

    auth_resolver(_Resolver(exc=dns.resolver.LifetimeTimeout()))
    monkeypatch.setattr(dns.resolver, "resolve", lambda *a, **k: ["from-system"])
    ans = dnscheck._query("x.acme.com", "TXT")
    assert ans.status == "ok"
    assert list(ans) == ["from-system"]


def test_authoritative_nxdomain_is_trusted_over_cache(
    real_dns_query, monkeypatch, auth_resolver
):
    """Bypassing negative caching is the point: don't second-guess NXDOMAIN."""
    import dns.resolver

    auth_resolver(_Resolver(exc=dns.resolver.NXDOMAIN()))
    called = []
    monkeypatch.setattr(dns.resolver, "resolve", lambda *a, **k: called.append(1))
    ans = dnscheck._query("x.acme.com", "TXT")
    assert ans.status == "nxdomain"
    assert called == []


def test_system_timeout_is_reported_as_timeout(real_dns_query, monkeypatch):
    import dns.resolver

    def _raise(*a, **k):
        raise dns.resolver.LifetimeTimeout()

    monkeypatch.setattr(dns.resolver, "resolve", _raise)
    assert dnscheck._query("x.acme.com", "TXT").status == "timeout"


def test_noanswer_confirmed_as_nxdomain_keeps_the_stronger_signal(
    real_dns_query, monkeypatch, auth_resolver
):
    import dns.resolver

    auth_resolver(_Resolver(exc=dns.resolver.NoAnswer()))

    def _nx(*a, **k):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", _nx)
    assert dnscheck._query("x.acme.com", "TXT").status == "nxdomain"


def test_noanswer_with_failed_confirmation_is_never_upgraded_to_missing_by_timeout(
    real_dns_query, monkeypatch, auth_resolver
):
    """A timed-out confirmation keeps the authoritative NoAnswer, not a timeout."""
    import dns.resolver

    auth_resolver(_Resolver(exc=dns.resolver.NoAnswer()))

    def _timeout(*a, **k):
        raise dns.resolver.LifetimeTimeout()

    monkeypatch.setattr(dns.resolver, "resolve", _timeout)
    assert dnscheck._query("x.acme.com", "TXT").status == "noanswer"


def test_root_spf_check_asks_the_authoritative_nameservers(monkeypatch):
    """No caller can accidentally get a cached answer for the root SPF advice."""
    import contextlib

    zones = []

    @contextlib.contextmanager
    def _auth(zone):
        zones.append(zone)
        yield

    monkeypatch.setattr(dnscheck, "authoritative", _auth)
    _patch(monkeypatch)
    dnscheck.check_root_spf("mail.acme.com")
    dnscheck.check_root_spf("mail.acme.com", "acme.com")
    assert zones == ["acme.com", "acme.com"]


def test_failed_zone_detection_is_cached_briefly_as_a_guess(
    real_detect_zone, monkeypatch
):
    """A resolver outage must not redo the failing SOA walk on every refresh."""
    import dns.resolver
    from django.core.cache import cache

    cache.clear()
    calls = []

    def _fail(*a, **k):
        calls.append(1)
        raise dns.resolver.NoNameservers()

    monkeypatch.setattr(dns.resolver, "zone_for_name", _fail)
    first = dnscheck.detect_zone("mail.acme.co.zm")
    second = dnscheck.detect_zone("mail.acme.co.zm")
    assert first.guessed and first.zone == "acme.co.zm"
    assert second.guessed and second.zone == "acme.co.zm"
    assert calls == [1]
    cache.clear()

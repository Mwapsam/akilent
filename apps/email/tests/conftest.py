"""Keep every email test off the network.

All DNS lookups in apps.email.dnscheck go through ``_query``; by default it
answers "nothing here" (NXDOMAIN), so an unpatched lookup reads as an absent
record rather than hitting real nameservers. Tests that need records patch
``_resolve_txt`` / ``_resolve_cname`` / ``_resolve_mx`` / ``_resolve_a`` as
before, and those patches take precedence. Zone detection returns the offline
guess.

Tests that exercise the lookup machinery itself use the ``real_dns_query``
fixture to get the original ``_query`` back.
"""

import pytest

from apps.email import dnscheck, dnshost

_REAL_QUERY = dnscheck._query
_REAL_DETECT_ZONE = dnscheck.detect_zone


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch, settings):
    settings.DNSCHECK_AUTHORITATIVE = False
    monkeypatch.setattr(
        dnscheck,
        "_query",
        lambda name, rdtype: dnscheck.Answer((), dnscheck.NXDOMAIN),
    )
    monkeypatch.setattr(
        dnscheck,
        "detect_zone",
        lambda domain: dnscheck.ZoneInfo(zone=dnshost.guess_zone(domain), guessed=True),
    )


@pytest.fixture
def real_dns_query(monkeypatch):
    """Restore the real ``_query`` for tests of the resolver logic itself."""
    monkeypatch.setattr(dnscheck, "_query", _REAL_QUERY)
    return _REAL_QUERY


@pytest.fixture
def real_detect_zone(monkeypatch):
    """Restore the real ``detect_zone`` (zone lookups themselves stay patched)."""
    monkeypatch.setattr(dnscheck, "detect_zone", _REAL_DETECT_ZONE)
    return _REAL_DETECT_ZONE

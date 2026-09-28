"""Pure helpers for presenting DNS records in the form a tenant's DNS host expects.

No network access lives here — see apps.email.dnscheck for lookups. Everything
in this module is presentation: which part of a record name to type into a
"Host" field, and which set of instructions to show first. Nothing here may
influence whether a record is judged correct; the diagnosis in dnscheck is
deliberately independent of who hosts the DNS.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Second-level labels that sit under a two-letter country code, e.g. acme.co.zm,
# acme.co.uk, acme.com.au. Used only when a live zone lookup isn't available.
_SLD_UNDER_CC = {
    "co",
    "com",
    "net",
    "org",
    "ac",
    "gov",
    "edu",
    "ltd",
    "plc",
    "or",
    "ne",
    "go",
}

# (slug, display name, nameserver domains). A nameserver matches a domain when
# it *is* that domain or ends with "." + it -- label-boundary suffix matching,
# so "cloudflare.com" matches "ada.ns.cloudflare.com" but never
# "notcloudflare.com". Route 53 is matched separately (see _ROUTE53), because
# its nameservers are ns-<n>.awsdns-<nn>.<tld>: the distinctive part is a label,
# not a suffix.
PROVIDERS: list[tuple[str, str, tuple[str, ...]]] = [
    ("cloudflare", "Cloudflare", ("cloudflare.com",)),
    ("godaddy", "GoDaddy", ("domaincontrol.com",)),
    ("namecheap", "Namecheap", ("registrar-servers.com",)),
    ("route53", "Amazon Route 53", ()),
    ("google", "Google / Squarespace", ("googledomains.com", "squarespacedns.com")),
    ("digitalocean", "DigitalOcean", ("digitalocean.com",)),
    (
        "azure",
        "Azure DNS",
        ("azure-dns.com", "azure-dns.net", "azure-dns.org", "azure-dns.info"),
    ),
    ("hostinger", "Hostinger", ("dns-parking.com",)),
]

# e.g. ns-1234.awsdns-12.co.uk -> a label exactly "awsdns-<digits>".
_ROUTE53 = re.compile(r"(^|\.)awsdns-\d+\.")

OTHER = "other"
_DISPLAY = {slug: name for slug, name, _ in PROVIDERS} | {OTHER: "Other DNS host"}


def _labels(name: str) -> list[str]:
    return [p for p in (name or "").strip().rstrip(".").lower().split(".") if p]


def guess_zone(domain: str) -> str:
    """Best-effort registered domain (zone apex) without a DNS lookup.

    ``mail.acme.com`` -> ``acme.com``; ``mail.acme.co.zm`` -> ``acme.co.zm``.
    A live SOA walk (dnscheck.detect_zone) is preferred; this is the fallback.
    """
    labels = _labels(domain)
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SLD_UNDER_CC:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def relative_host(name: str, zone: str) -> str:
    """What to type in a DNS host's Name/Host field when it appends the zone.

    ``bounce.mail.acme.com`` in zone ``acme.com`` -> ``bounce.mail``;
    the apex itself -> ``@``. A name outside the zone is returned in full.
    """
    name_l = ".".join(_labels(name))
    zone_l = ".".join(_labels(zone))
    if not name_l:
        return "@"
    if not zone_l:
        return name_l
    if name_l == zone_l:
        return "@"
    suffix = "." + zone_l
    if name_l.endswith(suffix):
        return name_l[: -len(suffix)]
    return name_l


def _under(ns: str, domain: str) -> bool:
    return ns == domain or ns.endswith("." + domain)


def detect_host(nameservers: Iterable[str] | None) -> str:
    """Map a zone's NS names to a provider slug, or ``"other"``."""
    ns = [n.lower().rstrip(".") for n in (nameservers or []) if n]
    for slug, _name, domains in PROVIDERS:
        for n in ns:
            if slug == "route53":
                if _ROUTE53.search(n):
                    return slug
            elif any(_under(n, d) for d in domains):
                return slug
    return OTHER


def display_name(slug: str) -> str:
    return _DISPLAY.get(slug or OTHER, _DISPLAY[OTHER])


def ordered_tabs(slug: str) -> list[tuple[str, str]]:
    """Instruction tabs, detected host first, generic steps always present.

    Detection only changes the order — every tab is always available, and an
    unknown host falls back to the generic steps rather than to any provider.
    """
    tabs = [(s, n) for s, n, _ in PROVIDERS] + [(OTHER, _DISPLAY[OTHER])]
    if slug and slug != OTHER:
        tabs.sort(key=lambda t: t[0] != slug)
    else:
        tabs.sort(key=lambda t: t[0] != OTHER)
    return tabs

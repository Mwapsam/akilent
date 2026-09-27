"""Live DNS verification and diagnosis for sending domains.

Queries the customer's published records and reports, per record, whether the
expected value is present -- and when it isn't, *why*: not added yet, added
under a doubled or wrong name, wrong value, two SPF records, proxied, wrong
type, or simply that the DNS host didn't answer.

Two rules this module keeps:

* **A failed lookup is never "missing".** A timeout / SERVFAIL means we don't
  know, and telling a tenant to add a record they already added is exactly the
  confusion this exists to remove. Lookups carry a status for that reason.
* **The diagnosis is DNS facts only.** It never depends on who hosts the DNS;
  apps.email.dnshost uses the detected host purely to order instructions.

Lookups go to the zone's own authoritative nameservers first, so a record the
tenant has just added shows up immediately instead of being hidden by a cached
"doesn't exist" answer; they fall back to the system resolver on timeout or
SERVFAIL. ``_resolve_*`` are the network seams, so tests monkeypatch them --
a patched function may return a plain list, and ``[]`` then means "absent".

The spec being checked comes from ``EmailDomain.dns_records()``.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
from dataclasses import dataclass, field

import dns.exception
import dns.resolver
from django.conf import settings
from django.core.cache import cache

from apps.email import dnshost

logger = logging.getLogger(__name__)

# Keep checks snappy — DNS that isn't there yet should fail fast, not hang the
# request or the auto-poll.
_LIFETIME = 5.0
_AUTH_LIFETIME = 3.0

OK, NXDOMAIN, NOANSWER, TIMEOUT, SERVFAIL = "ok", "nxdomain", "noanswer", "timeout", "servfail"
_UNKNOWN = {TIMEOUT, SERVFAIL}  # we don't know -- never report as missing

# The authoritative resolver for the zone being checked, if any.
_AUTH: contextvars.ContextVar = contextvars.ContextVar("dnscheck_auth", default=None)


class Answer(list):
    """A lookup result: the values found plus how the lookup ended."""

    def __init__(self, items=(), status: str = OK):
        super().__init__(items)
        self.status = status


def _status(ans) -> str:
    """How a lookup ended. A plain list (e.g. a test fake) is ok-or-absent."""
    status = getattr(ans, "status", None)
    if status:
        return status
    return OK if ans else NXDOMAIN


# ── Lookups ───────────────────────────────────────────────────────────────────

def _run(resolver, name: str, rdtype: str) -> Answer:
    try:
        if resolver is None:
            answers = dns.resolver.resolve(name, rdtype, lifetime=_LIFETIME)
        else:
            answers = resolver.resolve(name, rdtype, lifetime=_AUTH_LIFETIME)
    except dns.resolver.NXDOMAIN:
        return Answer((), NXDOMAIN)
    except dns.resolver.NoAnswer:
        return Answer((), NOANSWER)
    except (dns.resolver.LifetimeTimeout, dns.exception.Timeout):
        logger.debug("%s lookup for %s timed out", rdtype, name)
        return Answer((), TIMEOUT)
    except Exception as exc:  # NoNameservers, REFUSED, network errors, …
        logger.debug("%s lookup for %s failed: %s", rdtype, name, exc)
        return Answer((), SERVFAIL)
    return Answer(list(answers), OK)


def _query(name: str, rdtype: str) -> Answer:
    """Ask the zone's authoritative nameservers, else the system resolver.

    Authoritative ok/NXDOMAIN answers are trusted as-is: bypassing negative
    caching is the point. A NoAnswer is confirmed once via the system resolver
    (it can also mean a sub-zone delegation). Timeout/SERVFAIL falls back.
    """
    resolver = _AUTH.get()
    if resolver is not None:
        ans = _run(resolver, name, rdtype)
        if ans.status in (OK, NXDOMAIN):
            return ans
        if ans.status == NOANSWER:
            # Keep the stronger signal: a found record, or a confirmed
            # NXDOMAIN. A failed confirmation leaves the authoritative answer.
            confirmed = _run(None, name, rdtype)
            return confirmed if confirmed.status in (OK, NXDOMAIN) else ans
    return _run(None, name, rdtype)


def _resolve_txt(name: str) -> Answer:
    """TXT values published at ``name``, each fully concatenated."""
    ans = _query(name, "TXT")
    values = []
    for rdata in ans:
        parts = [
            p.decode("utf-8", "ignore") if isinstance(p, bytes) else str(p)
            for p in rdata.strings
        ]
        values.append("".join(parts))
    return Answer(values, ans.status)


def _resolve_cname(name: str) -> Answer:
    """CNAME target(s) at ``name`` (trailing dot kept)."""
    ans = _query(name, "CNAME")
    return Answer([str(r.target) for r in ans], ans.status)


def _resolve_mx(name: str) -> Answer:
    """``(preference, host)`` pairs at ``name``."""
    ans = _query(name, "MX")
    return Answer([(int(r.preference), str(r.exchange)) for r in ans], ans.status)


def _resolve_a(name: str) -> Answer:
    """A (then AAAA) addresses at ``name``."""
    ans = _query(name, "A")
    if not ans:
        six = _query(name, "AAAA")
        if six:
            ans = six
    return Answer([str(r) for r in ans], ans.status)


def _resolve_ns(name: str) -> Answer:
    ans = _query(name, "NS")
    return Answer([str(r.target).rstrip(".") for r in ans], ans.status)


# ── Zone + authoritative context ──────────────────────────────────────────────

@dataclass(frozen=True)
class ZoneInfo:
    zone: str
    nameservers: tuple = ()
    host: str = ""
    guessed: bool = False


def detect_zone(domain: str) -> ZoneInfo:
    """Find the zone apex (SOA walk) and who hosts it. Never raises."""
    key = f"dnszone:{domain.lower()}"
    cached = cache.get(key)
    if cached:
        return ZoneInfo(**cached)
    try:
        zone = dns.resolver.zone_for_name(domain, lifetime=4).to_text().rstrip(".")
        ns = tuple(_resolve_ns(zone))
        info = ZoneInfo(zone=zone, nameservers=ns, host=dnshost.detect_host(ns))
        ttl = 6 * 3600
    except Exception as exc:
        logger.debug("zone detection for %s failed: %s", domain, exc)
        # Cache the guess briefly so a resolver outage doesn't redo the failing
        # SOA walk on every refresh; it's marked guessed, so it's never persisted.
        info = ZoneInfo(zone=dnshost.guess_zone(domain), guessed=True)
        ttl = 600
    cache.set(key, {"zone": info.zone, "nameservers": info.nameservers,
                    "host": info.host, "guessed": info.guessed}, ttl)
    return info


def _zone_ns_ips(zone: str) -> list[str]:
    key = f"dnsauth:{zone}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    ips: list[str] = []
    for ns in list(_resolve_ns(zone))[:2]:
        ips.extend(_resolve_a(ns))
    # Don't pin a transient failure for an hour.
    cache.set(key, ips, 3600 if ips else 60)
    return ips


@contextlib.contextmanager
def authoritative(zone: str):
    """Route lookups inside the block to ``zone``'s own nameservers."""
    if not zone or not getattr(settings, "DNSCHECK_AUTHORITATIVE", True):
        yield
        return
    try:
        ips = _zone_ns_ips(zone)
    except Exception:
        ips = []
    if not ips:
        yield
        return
    resolver = dns.resolver.Resolver(configure=False)
    resolver.nameservers = ips
    resolver.timeout = 2.0
    resolver.lifetime = _AUTH_LIFETIME
    token = _AUTH.set(resolver)
    try:
        yield
    finally:
        _AUTH.reset(token)


# ── Diagnosis ─────────────────────────────────────────────────────────────────

@dataclass
class Diag:
    ok: bool
    code: str
    message: str = ""
    found: list = field(default_factory=list)
    suggestion: str = ""

    def as_dict(self) -> dict:
        d = {"code": self.code, "message": self.message, "found": list(self.found)}
        if self.suggestion:
            d["suggestion"] = self.suggestion
        return d


def _norm(s: str) -> str:
    """Lower-case and strip all whitespace — for tolerant value comparison."""
    return re.sub(r"\s+", "", s or "").lower()


def _contains(name: str, needle: str) -> bool:
    """Whether any TXT value at ``name`` contains ``needle`` (whitespace-insensitive)."""
    if not needle:
        return False
    target = _norm(needle)
    return any(target in _norm(v) for v in _resolve_txt(name))


def _dkim_public_key(value: str) -> str:
    """Extract the base64 ``p=`` portion of a DKIM TXT value, if present."""
    m = re.search(r"p=([A-Za-z0-9+/=]+)", value or "")
    return m.group(1) if m else ""


def _host(name: str) -> str:
    return (name or "").strip().rstrip(".").lower()


def _merge_spf(records: list[str], required: list[str]) -> str:
    """One SPF record combining every mechanism from ``records`` + ``required``."""
    mechanisms, all_term = [], ""
    for rec in records:
        for term in rec.split()[1:]:
            if term.lower().endswith("all"):
                all_term = all_term or term
            elif term.lower() not in (m.lower() for m in mechanisms):
                mechanisms.append(term)
    for inc in required:
        if inc.lower() not in (m.lower() for m in mechanisms):
            mechanisms.append(inc)
    return " ".join(["v=spf1", *mechanisms, all_term or "~all"])


_TIMEOUT_MSG = (
    "Your DNS host didn't answer when we checked, so we couldn't tell whether "
    "this record is there. We'll check again automatically."
)
_MISSING_MSG = "Not added yet."


def _timeout() -> Diag:
    return Diag(False, "timeout", _TIMEOUT_MSG)


def _present_at(name: str, rtype: str) -> list:
    """Anything of the expected record type at ``name`` (for probes)."""
    if rtype == "CNAME":
        return list(_resolve_cname(name))
    if rtype == "MX":
        return [f"{p} {h}" for p, h in _resolve_mx(name)]
    return list(_resolve_txt(name))


def _probe_misplaced(row: dict, zone: str, domain: str) -> Diag | None:
    """Was the record added under the wrong name? Only run when it's missing."""
    name, rtype = _host(row["name"]), (row.get("type") or "TXT").upper()
    zone, domain = _host(zone), _host(domain)
    host = dnshost.relative_host(name, zone)

    if zone:
        doubled = f"{name}.{zone}"
        found = _present_at(doubled, rtype)
        if found:
            return Diag(
                False, "doubled",
                f"We found this record at {doubled} — your DNS host adds "
                f"\"{zone}\" to the name automatically. Change the Name to "
                f"\"{host}\".",
                found,
            )

    if domain and zone and domain != zone and name.endswith("." + domain):
        short = name[: -len(domain)] + zone
        found = _present_at(short, rtype)
        if found:
            return Diag(
                False, "wrong_name",
                f"This was added as {short}, but it must be {name}. "
                f"Change the Name to \"{host}\".",
                found,
            )
    return None


def _missing(row, zone, domain) -> Diag:
    return _probe_misplaced(row, zone, domain) or Diag(False, "missing", _MISSING_MSG)


def _diagnose_spf(row: dict, zone: str, domain: str) -> Diag:
    name = row["name"]
    txt = _resolve_txt(name)
    if _status(txt) in _UNKNOWN:
        return _timeout()
    spfs = [v for v in txt if _norm(v).startswith("v=spf1")]
    required = re.findall(r"include:[a-z0-9._-]+", _norm(row.get("value") or ""))

    if len(spfs) > 1:
        return Diag(
            False, "multiple_spf",
            f"There are {len(spfs)} SPF records at {name}. A name may have only "
            "one: receivers treat two as an error and SPF fails for all mail. "
            "Replace them with the single combined record below.",
            spfs, _merge_spf(spfs, required),
        )
    if len(spfs) == 1:
        have = _norm(spfs[0])
        missing_inc = [inc for inc in required if inc not in have]
        if not missing_inc:
            return Diag(True, "ok")
        return Diag(
            False, "wrong_value",
            f"Your SPF record is missing {' '.join(missing_inc)}. Edit the "
            "existing record rather than adding a second one.",
            spfs, _merge_spf(spfs, required),
        )
    if row.get("key") == "mfspf" and _resolve_cname(name):
        return _cname_conflict(name)
    return _missing(row, zone, domain)


def _cname_conflict(name: str) -> Diag:
    return Diag(
        False, "cname_conflict",
        f"There's a CNAME record at {name}. A name with a CNAME can't have any "
        "other records, so remove the CNAME and add these instead.",
        list(_resolve_cname(name)),
    )


def _diagnose_mx(row: dict, zone: str, domain: str) -> Diag:
    name, want = row["name"], _host(row.get("value"))
    mx = _resolve_mx(name)
    if _status(mx) in _UNKNOWN:
        return _timeout()
    found = [f"{p} {h}" for p, h in mx]
    if mx:
        hosts = [_host(h) for _p, h in mx]
        if want in hosts:
            return Diag(True, "ok")
        # "10 feedback-smtp…" typed into the server field.
        if any(re.sub(r"^\d+[\s.]+", "", h) == want for h in hosts):
            return Diag(
                False, "wrong_value",
                f"The priority ended up in the mail server value. Put "
                f"{row.get('priority') or 10} in the Priority field and only "
                f"{want} as the mail server.",
                found,
            )
        return Diag(
            False, "mx_mismatch",
            f"The MX record at {name} points somewhere else. It must point to {want}.",
            found,
        )
    if _resolve_cname(name):
        return _cname_conflict(name)
    return _missing(row, zone, domain)


def _diagnose_cname(row: dict, zone: str, domain: str) -> Diag:
    name, want = row["name"], _host(row.get("value"))
    if not want:
        return Diag(False, "missing", _MISSING_MSG)
    cn = _resolve_cname(name)
    if _status(cn) in _UNKNOWN:
        return _timeout()
    if cn:
        if want in (_host(v) for v in cn):
            return Diag(True, "ok")
        return Diag(
            False, "wrong_value",
            f"This points to the wrong place. It must point to {want}.",
            list(cn),
        )
    txt = _resolve_txt(name)
    if txt:
        return Diag(
            False, "wrong_type",
            "This was added as a TXT record, but it must be a CNAME record. "
            "Delete it and add it again with Type CNAME.",
            list(txt),
        )
    addrs = _resolve_a(name)
    if addrs:
        return Diag(
            False, "proxied",
            "Your DNS host is answering with an IP address instead of pointing "
            "to the value (proxying or CNAME flattening is on). Turn that off "
            "for this record so it resolves as a plain CNAME.",
            list(addrs),
        )
    return _missing(row, zone, domain)


def _diagnose_txt(row: dict, zone: str, domain: str) -> Diag:
    name, value, key = row["name"], row.get("value") or "", row.get("key")
    if not value:
        return Diag(False, "missing", _MISSING_MSG)
    txt = _resolve_txt(name)
    if _status(txt) in _UNKNOWN:
        return _timeout()
    if key == "dkim":
        # Legacy single-TXT DKIM: the published record must carry our p= key.
        pub = _dkim_public_key(value)
        needle = _norm(pub)
    else:
        needle = _norm(value)
    if needle and any(needle in _norm(v) for v in txt):
        return Diag(True, "ok")
    if key == "verify":
        near = [v for v in txt if "verification" in v.lower()]
        if near:
            return Diag(
                False, "wrong_value",
                "There's a verification record here, but not the current one. "
                "Copy the Value again exactly as shown.",
                near,
            )
    return _missing(row, zone, domain)


def _diagnose_dmarc(row: dict, zone: str, domain: str) -> Diag:
    txt = _resolve_txt(row["name"])
    if _status(txt) in _UNKNOWN:
        return _timeout()
    if any(_norm(v).startswith("v=dmarc1") for v in txt):
        return Diag(True, "ok")  # any DMARC1 policy counts
    return _missing(row, zone, domain)


def diagnose(row: dict, zone: str = "", domain: str = "") -> Diag:
    """Whether one record is live as specified, and if not, exactly why."""
    if not (row.get("name") or ""):
        return Diag(False, "missing", _MISSING_MSG)
    zone = zone or dnshost.guess_zone(domain or row["name"])
    key, rtype = row.get("key"), (row.get("type") or "TXT").upper()
    if key in ("spf", "mfspf"):
        return _diagnose_spf(row, zone, domain)
    if key == "dmarc":
        return _diagnose_dmarc(row, zone, domain)
    if rtype == "MX":
        return _diagnose_mx(row, zone, domain)
    if rtype == "CNAME":
        return _diagnose_cname(row, zone, domain)
    return _diagnose_txt(row, zone, domain)


def _record_present(row: dict) -> bool:
    """True when the single DNS record described by ``row`` is live as specified."""
    return diagnose(row).ok


def check_root_spf(domain: str, zone: str = "") -> dict | None:
    """Advice when the sending domain itself publishes more than one SPF record.

    Earlier Akilent instructions told tenants to add an SES SPF record at the
    root; on a domain that already had SPF for its inbox, that makes two.
    Asks the authoritative nameservers itself, like every other check, so no
    caller can accidentally get a cached answer.
    """
    with authoritative(zone or dnshost.guess_zone(domain)):
        spfs = [v for v in _resolve_txt(domain) if _norm(v).startswith("v=spf1")]
    if len(spfs) < 2:
        return None
    return Diag(
        False, "multiple_spf",
        f"{domain} has {len(spfs)} SPF records. A domain may have only one: "
        "receivers treat two as an error, so SPF fails for everything you send. "
        "Akilent doesn't need an SPF record here any more — if one of these is "
        "the \"include:amazonses.com\" record we asked for earlier, delete it.",
        spfs, _merge_spf(spfs, []),
    ).as_dict()


def check_records(record) -> list[dict]:
    """``record.dns_records()`` with each row's live ``ok`` and ``diag``."""
    zone = record.effective_zone
    with authoritative(zone):
        out = []
        for row in record.dns_records():
            d = diagnose(row, zone, record.domain)
            out.append({**row, "ok": d.ok, "diag": d.as_dict()})
    return out


def check_domain(record) -> dict:
    """``{key: found_bool}`` for each record key the domain actually has.

    A key with several rows (SES DKIM has three) is ``True`` only when every
    one of its rows is live.
    """
    by_key: dict[str, list[bool]] = {}
    for row in check_records(record):
        by_key.setdefault(row["key"], []).append(row["ok"])
    return {key: all(oks) for key, oks in by_key.items()}

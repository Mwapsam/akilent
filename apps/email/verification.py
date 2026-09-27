"""Shared domain-verification logic.

``refresh_domain`` runs the live DNS check for a sending domain, persists the
per-record readiness *and the reason for each failure*, keeps the record spec
and the custom MAIL FROM up to date, and transitions the domain
PENDING → VERIFIED once ownership is proven (and, for AWS SES, once SES itself
reports the identity verified). The ``domain_verify`` view and the
``reverify_pending_domains`` / ``recheck_verified_domains`` tasks all call it,
so the rules stay in one place.

What decides whether a domain is *usable* is unchanged: the ownership record
plus DKIM (and SES's own verdict). MAIL FROM is a separate deliverability layer
-- recommended, reported, never gating.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from apps.email import dnscheck
from apps.email.models import EmailDomain

logger = logging.getLogger(__name__)

_ZONE_TTL = timedelta(hours=24)
_MAIL_FROM_RETRY = timedelta(hours=24)


def _ses_identity_verified(record: EmailDomain, provider) -> bool:
    """Ask the mail provider whether it considers the identity verified.

    Tolerant: any provider error (unsupported, transient AWS failure) is treated
    as "not yet verified" rather than raising, so a flaky API call never blocks
    or crashes verification — the next poll retries.
    """
    try:
        if provider is None:
            from apps.email.providers import get_mail_provider

            provider = get_mail_provider()
        return bool(provider.verify_domain(record.domain).success)
    except Exception:
        logger.exception("provider.verify_domain failed for %s", record.domain)
        return False


def _refresh_zone(record: EmailDomain) -> list[str]:
    """Learn the zone apex and DNS host, at most daily. Returns fields changed."""
    fresh = (
        record.dns_zone_checked_at
        and timezone.now() - record.dns_zone_checked_at < _ZONE_TTL
    )
    if fresh:
        return []
    # A failed lookup isn't retried on every 20-second poll.
    if not cache.add(f"dnszonecheck:{record.pk}", 1, 600):
        return []
    try:
        info = dnscheck.detect_zone(record.domain)
    except Exception:
        logger.exception("zone detection failed for %s", record.domain)
        return []
    if info.guessed:
        return []  # keep using the offline guess; try again later
    record.dns_zone = info.zone
    record.dns_host = info.host
    record.dns_zone_checked_at = timezone.now()
    return ["dns_zone", "dns_host", "dns_zone_checked_at"]


def _service(record: EmailDomain, provider):
    from apps.email.services.domain import DomainService

    svc = DomainService(record.account)
    if provider is not None:
        svc._provider = provider
    return svc


def _ensure_spec(record: EmailDomain, provider) -> None:
    """Upgrade an existing SES domain to the current record spec (MAIL FROM)."""
    if not record.is_ses_backed():
        return
    try:
        _service(record, provider).ensure_dns_spec(record)
    except Exception:
        logger.exception("ensure_dns_spec failed for %s", record.domain)


def _refresh_mail_from_status(record: EmailDomain, provider) -> list[str]:
    """Mirror SES's MAIL FROM status; re-trigger it when SES gave up.

    SES stops re-checking a MAIL FROM domain once it reports FAILED, so if the
    tenant has since fixed their records we ask SES to try again -- at most once
    a day. Returns fields changed. Never raises.
    """
    if not record.mail_from_domain:
        return []
    ttl = 86400 if record.mail_from_status == "SUCCESS" else 300
    if not cache.add(f"mfstat:{record.pk}", 1, ttl):
        return []
    try:
        if provider is None:
            from apps.email.providers import get_mail_provider

            provider = get_mail_provider()
        get_mail_from = getattr(provider, "get_mail_from", None)
        if get_mail_from is None:
            return []
        info = get_mail_from(record.domain)
    except Exception:
        logger.exception("get_mail_from failed for %s", record.domain)
        return []
    if info is None:
        return []

    fields = []
    if info.status != record.mail_from_status:
        record.mail_from_status = info.status
        fields.append("mail_from_status")

    retry_due = (
        record.mail_from_attempted_at is None
        or timezone.now() - record.mail_from_attempted_at >= _MAIL_FROM_RETRY
    )
    if info.status == "FAILED" and record.mail_from_ok and retry_due:
        # ensure_mail_from saves its own fields and never raises.
        _service(record, provider).ensure_mail_from(record)
    return fields


def refresh_domain(record: EmailDomain, *, provider=None) -> dict:
    """Re-check DNS for ``record``, persist status, return ``{key: found}``.

    Side effects (all saved):
      * the zone / DNS host, at most daily (display only)
      * for SES: the record spec brought up to date (MAIL FROM, no root SPF)
      * ``is_ok`` / ``checked_at`` on each EmailDnsRecord row
      * ``dns_diagnostics``: the reason each failing record fails
      * rollups: ``dkim_ok`` / ``spf_ok`` / ``dmarc_ok`` / ``mail_from_ok``
      * the SES MAIL FROM status
      * ``status`` → VERIFIED + ``verified_at`` when ownership is satisfied
        (never downgraded)
    """
    if record.ensure_verification_token():
        record.save(update_fields=["verify_record_name", "verify_record_value"])

    fields = _refresh_zone(record)
    _ensure_spec(record, provider)

    rows = dnscheck.check_records(record)

    now = timezone.now()
    db_rows = {(r.key, r.name): r for r in record.dns_record_rows.all()} if record.pk else {}
    to_update = []
    for row in rows:
        db_row = db_rows.get((row["key"], row["name"]))
        if db_row is not None and (db_row.is_ok != row["ok"] or db_row.checked_at is None):
            db_row.is_ok = row["ok"]
            db_row.checked_at = now
            to_update.append(db_row)
    if to_update:
        from apps.email.models import EmailDnsRecord

        EmailDnsRecord.objects.bulk_update(to_update, ["is_ok", "checked_at"])

    # Persist why each record failed, so the card can explain without doing DNS.
    diagnostics = {
        f"{row['key']}|{row['name']}": row["diag"]
        for row in rows
        if row.get("diag")
    }
    try:
        advice = dnscheck.check_root_spf(record.domain, record.effective_zone)
    except Exception:
        logger.exception("root SPF check failed for %s", record.domain)
        advice = None
    if advice:
        diagnostics["_advice|root_spf"] = advice
    record.dns_diagnostics = diagnostics

    agg: dict[str, list[bool]] = {}
    for row in rows:
        agg.setdefault(row["key"], []).append(row["ok"])

    def _ok(key: str) -> bool:
        vals = agg.get(key) or []
        return bool(vals) and all(vals)

    record.dkim_ok = _ok("dkim")
    # SES evaluates SPF on the MAIL FROM subdomain, so that's the SPF that counts.
    record.spf_ok = _ok("mfspf") if "mfspf" in agg else _ok("spf")
    record.dmarc_ok = _ok("dmarc")
    record.mail_from_ok = _ok("mfmx") and _ok("mfspf")
    record.last_checked_at = now
    fields += [
        "dkim_ok", "spf_ok", "dmarc_ok", "mail_from_ok",
        "dns_diagnostics", "last_checked_at",
    ]

    fields += _refresh_mail_from_status(record, provider)

    if record.status != EmailDomain.Status.VERIFIED:
        ownership_ok = _ok("verify")
        if ownership_ok and record.is_ses_backed():
            # SES verifies a domain by checking the same DKIM CNAMEs; require
            # both our DNS check and SES's own status so we never mark a domain
            # sendable before SES will actually accept mail from it.
            ownership_ok = _ok("dkim") and _ses_identity_verified(record, provider)
        if ownership_ok:
            record.status = EmailDomain.Status.VERIFIED
            record.verified_at = now
            fields += ["status", "verified_at"]

    record.save(update_fields=list(dict.fromkeys(fields)))

    result = {k: _ok(k) for k in agg}
    result.setdefault("verify", False)
    return result

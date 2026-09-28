"""Stalwart x:Domain / x:Account / x:MailingList → normalized type adapters.

Pure functions that convert Stalwart's proprietary JMAP objects (capability
``urn:stalwart:jmap``) into provider-agnostic dataclasses from apps.email.types.
No HTTP calls, no Django ORM — just data mapping.

x:Domain, x:Account and x:MailingList shapes are all confirmed live
(round-tripped create/get against the production server). These adapters
still read defensively and fall back to sane defaults rather than raising,
since Stalwart's schemas allow most fields to be null/absent.
"""

from __future__ import annotations

from typing import Any

from apps.email.types import (
    AliasInfo,
    DkimRecord,
    DomainInfo,
    DomainStatus,
    MailboxInfo,
    MailboxStatus,
    OperationResult,
    QuotaInfo,
)


def _bytes_to_mb(value: int | float | None) -> int:
    if not value:
        return 0
    return int(value) // (1024 * 1024)


# ── Domain adapters ───────────────────────────────────────────────────────────


def adapt_domain_object(
    obj: dict[str, Any], domain_name: str, dkim: DkimRecord | None = None
) -> DomainInfo:
    is_enabled = obj.get("isEnabled")
    status = (
        DomainStatus.ACTIVE
        if (is_enabled is None or is_enabled)
        else DomainStatus.DISABLED
    )
    return DomainInfo(
        domain=obj.get("name") or domain_name,
        status=status,
        dkim=dkim,
        max_accounts=None,  # not exposed by x:Domain; enforced in Django billing
        disk_quota_mb=None,  # not exposed by x:Domain; enforced in Django billing
        description=obj.get("description") or "",
    )


def adapt_dkim_from_signature(sig: dict[str, Any], domain: str) -> DkimRecord:
    """Build a DKIM TXT record straight from an x:DkimSignature object.

    x:Domain.dnsManagement.dnsZoneFile (pre-rendered zone-file text) is never
    populated via the JMAP API — confirmed empty even on progstack.org after
    a day in production — so the signature object's own ``selector`` /
    ``publicKey`` / ``@type`` fields are the only reliable source for the
    DKIM DNS TXT value.
    """
    sel = sig.get("selector") or ""
    sig_type = sig.get("@type") or ""
    public_key = sig.get("publicKey") or ""
    is_ed25519 = "Ed25519" in sig_type
    algorithm = "ed25519-sha256" if is_ed25519 else "rsa-sha256"
    k = "ed25519" if is_ed25519 else "rsa"
    return DkimRecord(
        selector=sel,
        algorithm=algorithm,
        public_key_txt=f"v=DKIM1; k={k}; p={public_key}",
        record_name=f"{sel}._domainkey.{domain}",
    )


def choose_dkim_signature(
    sigs: list[dict[str, Any]], selector: str
) -> dict[str, Any] | None:
    """Pick the signature matching ``selector``, else prefer RSA, else first.

    Stalwart's real selectors look like ``v1-rsa-20260629`` — Django's
    ``EmailDomain.dkim_selector`` default ("dkim") won't substring-match
    those, so the RSA-preference fallback is what actually fires in practice.
    """
    if not sigs:
        return None
    chosen = next(
        (s for s in sigs if selector and selector in (s.get("selector") or "")), None
    )
    if chosen is None:
        chosen = next((s for s in sigs if "Rsa" in (s.get("@type") or "")), sigs[0])
    return chosen


# ── Mailbox (x:Account) adapters ────────────────────────────────────────────


def adapt_account_object(obj: dict[str, Any], email: str) -> MailboxInfo:
    quotas = obj.get("quotas") or {}
    quota_limit = quotas.get("maxDiskQuota") or 0
    used = obj.get("usedDiskQuota") or 0
    quota = QuotaInfo(used_mb=_bytes_to_mb(used), limit_mb=_bytes_to_mb(quota_limit))

    permissions = obj.get("permissions") or {}
    disabled = permissions.get("@type") == "Replace" and not permissions.get(
        "enabledPermissions"
    )
    status = MailboxStatus.SUSPENDED if disabled else MailboxStatus.ACTIVE

    roles = obj.get("roles") or {}
    is_admin = roles.get("@type") == "Admin"

    return MailboxInfo(
        email=obj.get("emailAddress") or email,
        name=obj.get("name") or "",
        status=status,
        quota=quota,
        is_admin=is_admin,
        description=obj.get("description") or "",
    )


def adapt_quota_account(obj: dict[str, Any]) -> QuotaInfo:
    quotas = obj.get("quotas") or {}
    return QuotaInfo(
        used_mb=_bytes_to_mb(obj.get("usedDiskQuota") or 0),
        limit_mb=_bytes_to_mb(quotas.get("maxDiskQuota") or 0),
    )


# ── Alias (x:MailingList) adapters ──────────────────────────────────────────


def adapt_mailing_list_object(obj: dict[str, Any], address: str) -> AliasInfo:
    targets = list((obj.get("recipients") or {}).keys())
    return AliasInfo(
        address=obj.get("emailAddress") or address,
        targets=list(targets),
        is_active=True,
        description=obj.get("description") or "",
    )


# ── Result helpers ────────────────────────────────────────────────────────────


def ok(message: str = "", **metadata: Any) -> OperationResult:
    """Convenience factory for a successful OperationResult."""
    return OperationResult(success=True, message=message, metadata=dict(metadata))

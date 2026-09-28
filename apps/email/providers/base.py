"""Provider-agnostic EmailProvider interface.

Any mail server backend (Stalwart, Mailcow, Google Workspace, Exchange, ...)
is supported by implementing this ABC. Business logic imports only from here
and from apps.email.types — never from a concrete provider module.

Design principle: every method returns a typed dataclass from apps.email.types,
never a raw dict. Adapters belong in the provider, not scattered across the
service layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from apps.email.exceptions import EmailProviderError
from apps.email.types import (
    DkimRecord,
    DomainInfo,
    MailFromInfo,
    OperationResult,
)

# ── Backwards-compatible shims ────────────────────────────────────────────────
# Existing code imported MailProviderError, DkimResult, ProvisionResult from
# here. Keeping them so no view or task import breaks during the migration.

MailProviderError = EmailProviderError


@dataclass
class DkimResult:
    """Legacy wrapper — new code should use DkimRecord from apps.email.types."""

    selector: str
    dkim_txt: str  # full TXT value ready for DNS: "v=DKIM1; k=rsa; p=..."


@dataclass
class ProvisionResult:
    """Legacy wrapper — new code should use DomainInfo from apps.email.types."""

    dkim: DkimResult


# ── Abstract interface ────────────────────────────────────────────────────────


class EmailProvider(ABC):
    """Abstract interface for a mail server infrastructure backend.

    Implement all abstract methods to add a new provider. The factory in
    apps.email.providers.__init__ resolves the concrete class at runtime via
    the MAIL_PROVIDER_BACKEND Django setting.

    Threading: instances are not thread-safe. Instantiate one per request,
    per Celery task, or per service call.
    """

    # ── Domain lifecycle ──────────────────────────────────────────────────

    @abstractmethod
    def create_domain(
        self,
        domain: str,
        *,
        max_accounts: int | None = None,
        disk_quota_mb: int | None = None,
        description: str = "",
    ) -> DomainInfo:
        """Provision a new domain and generate its DKIM keypair.

        Returns DomainInfo with .dkim populated so the caller can display
        the DNS TXT value to the tenant immediately after provisioning.
        The DKIM private key stays on the mail server.
        """

    @abstractmethod
    def delete_domain(self, domain: str) -> OperationResult:
        """Permanently remove a domain and all its accounts/aliases/DKIM keys."""

    # ── Optional mail-server operations ───────────────────────────────────
    # Not every backend is a full mail server. API-style providers (e.g. AWS
    # SES) manage a sending identity + DKIM only and have no concept of
    # per-domain account limits, disk quotas, or operator-chosen DKIM
    # selectors. Those providers leave the methods below at the default, which
    # raises so a mis-wired call site fails loudly instead of silently no-op'ing.

    def get_domain(self, domain: str) -> DomainInfo:
        """Fetch current metadata for an existing domain."""
        raise EmailProviderError("get_domain is not supported by this provider")

    def update_domain(
        self,
        domain: str,
        *,
        max_accounts: int | None = None,
        disk_quota_mb: int | None = None,
        description: str | None = None,
    ) -> DomainInfo:
        """Update mutable domain settings."""
        raise EmailProviderError("update_domain is not supported by this provider")

    def list_domains(self) -> list[DomainInfo]:
        """Return all domains configured on the mail server."""
        raise EmailProviderError("list_domains is not supported by this provider")

    def set_domain_active(self, domain: str, *, active: bool) -> OperationResult:
        """Enable or disable a domain without deleting it."""
        raise EmailProviderError("set_domain_active is not supported by this provider")

    # ── DKIM management ───────────────────────────────────────────────────

    def provision_dkim(
        self,
        domain: str,
        *,
        selector: str = "dkim",
        algorithm: str = "rsa-sha256",
    ) -> DkimRecord:
        """Generate a DKIM keypair for the domain on the mail server.

        The private key stays on the server. Returns the public-key TXT record
        for storage in Django and display to the tenant.
        """
        raise EmailProviderError("provision_dkim is not supported by this provider")

    @abstractmethod
    def get_dkim(self, domain: str, *, selector: str = "dkim") -> DkimRecord:
        """Retrieve the current DKIM public-key TXT record."""

    def rotate_dkim(
        self,
        domain: str,
        *,
        new_selector: str,
        algorithm: str = "rsa-sha256",
    ) -> DkimRecord:
        """Generate a new DKIM keypair under a different selector.

        The old selector remains valid during DNS propagation. Callers should
        schedule old-key deletion after confirming the new record is live in DNS.
        """
        raise EmailProviderError("rotate_dkim is not supported by this provider")

    # ── Custom MAIL FROM (optional capability) ────────────────────────────
    # Unlike the operations above, these don't raise when unsupported: a custom
    # return-path is a deliverability enhancement, never a requirement, so a
    # backend without it simply returns None and callers carry on.

    def configure_mail_from(
        self, domain: str, *, subdomain: str = "bounce"
    ) -> MailFromInfo | None:
        """Point the domain's return-path at ``{subdomain}.{domain}``.

        Returns the requested configuration, or None if unsupported.
        """
        return None

    def get_mail_from(self, domain: str) -> MailFromInfo | None:
        """The provider's current MAIL FROM state, or None if unsupported/unset."""
        return None

    # ── Legacy compatibility helpers ──────────────────────────────────────
    # Concrete implementations of the old 8-method interface so existing
    # views and tasks continue to work unchanged while the migration proceeds.

    def provision_domain(self, domain: str, selector: str = "dkim") -> ProvisionResult:
        """Legacy shim: wraps create_domain() in the old ProvisionResult shape."""
        info = self.create_domain(domain)
        dkim = info.dkim
        if dkim is None:
            dkim = self.provision_dkim(domain, selector=selector)
        return ProvisionResult(
            dkim=DkimResult(selector=dkim.selector, dkim_txt=dkim.public_key_txt)
        )


# Alias so old imports of MailProvider still resolve.
MailProvider = EmailProvider

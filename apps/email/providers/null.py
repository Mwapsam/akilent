"""No-op mail provider for local development and CI.

Set MAIL_PROVIDER_BACKEND=null in .env to run the full application without a
live mail server. All provisioning calls succeed silently; DKIM is a stable
placeholder value so domain creation completes and DNS records can be displayed.

Never use this in production — no mail is actually delivered or provisioned.
"""

from __future__ import annotations

import logging

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

from .base import EmailProvider

logger = logging.getLogger(__name__)

_PLACEHOLDER_DKIM_TXT = (
    "v=DKIM1; k=rsa; "
    "p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC_NULL_PROVIDER_PLACEHOLDER_QIDAQAB"
)

_PLACEHOLDER_QUOTA = QuotaInfo(used_mb=0, limit_mb=1024)


def _ok(msg: str = "") -> OperationResult:
    return OperationResult(success=True, message=msg)


class NullProvider(EmailProvider):
    """No-op stubs for every EmailProvider method. Safe in CI and local dev."""

    # ── Domain lifecycle ──────────────────────────────────────────────────

    def create_domain(
        self,
        domain: str,
        *,
        max_accounts: int | None = None,
        disk_quota_mb: int | None = None,
        description: str = "",
    ) -> DomainInfo:
        logger.debug("NullProvider.create_domain(%s)", domain)
        return DomainInfo(
            domain=domain,
            status=DomainStatus.ACTIVE,
            dkim=DkimRecord(
                selector="dkim",
                algorithm="rsa-sha256",
                public_key_txt=_PLACEHOLDER_DKIM_TXT,
                record_name=f"dkim._domainkey.{domain}",
            ),
            max_accounts=max_accounts,
            disk_quota_mb=disk_quota_mb,
            description=description,
        )

    def get_domain(self, domain: str) -> DomainInfo:
        logger.debug("NullProvider.get_domain(%s)", domain)
        return DomainInfo(domain=domain, status=DomainStatus.ACTIVE)

    def update_domain(
        self,
        domain: str,
        *,
        max_accounts: int | None = None,
        disk_quota_mb: int | None = None,
        description: str | None = None,
    ) -> DomainInfo:
        logger.debug("NullProvider.update_domain(%s)", domain)
        return DomainInfo(domain=domain, status=DomainStatus.ACTIVE)

    def delete_domain(self, domain: str) -> OperationResult:
        logger.debug("NullProvider.delete_domain(%s)", domain)
        return _ok(f"Domain {domain} deleted (null).")

    def list_domains(self) -> list[DomainInfo]:
        logger.debug("NullProvider.list_domains()")
        return []

    def set_domain_active(self, domain: str, *, active: bool) -> OperationResult:
        logger.debug("NullProvider.set_domain_active(%s, %s)", domain, active)
        return _ok()

    # ── DKIM management ───────────────────────────────────────────────────

    def provision_dkim(
        self,
        domain: str,
        *,
        selector: str = "dkim",
        algorithm: str = "rsa-sha256",
    ) -> DkimRecord:
        logger.debug("NullProvider.provision_dkim(%s, %s)", domain, selector)
        return DkimRecord(
            selector=selector,
            algorithm=algorithm,
            public_key_txt=_PLACEHOLDER_DKIM_TXT,
            record_name=f"{selector}._domainkey.{domain}",
        )

    def get_dkim(self, domain: str, *, selector: str = "dkim") -> DkimRecord:
        logger.debug("NullProvider.get_dkim(%s, %s)", domain, selector)
        return DkimRecord(
            selector=selector,
            algorithm="rsa-sha256",
            public_key_txt=_PLACEHOLDER_DKIM_TXT,
            record_name=f"{selector}._domainkey.{domain}",
        )

    def rotate_dkim(
        self,
        domain: str,
        *,
        new_selector: str,
        algorithm: str = "rsa-sha256",
    ) -> DkimRecord:
        logger.debug("NullProvider.rotate_dkim(%s, %s)", domain, new_selector)
        return DkimRecord(
            selector=new_selector,
            algorithm=algorithm,
            public_key_txt=_PLACEHOLDER_DKIM_TXT,
            record_name=f"{new_selector}._domainkey.{domain}",
        )

    # ── Mailbox lifecycle ─────────────────────────────────────────────────

    def create_mailbox(
        self,
        email: str,
        password: str,
        *,
        name: str = "",
        quota_mb: int | None = None,
        description: str = "",
    ) -> MailboxInfo:
        logger.debug("NullProvider.create_mailbox(%s)", email)
        return MailboxInfo(
            email=email,
            name=name or email.rsplit("@", 1)[0],
            status=MailboxStatus.ACTIVE,
            quota=QuotaInfo(used_mb=0, limit_mb=quota_mb or 1024),
            description=description,
        )

    def get_mailbox(self, email: str) -> MailboxInfo:
        logger.debug("NullProvider.get_mailbox(%s)", email)
        return MailboxInfo(
            email=email,
            name=email.rsplit("@", 1)[0],
            status=MailboxStatus.ACTIVE,
            quota=_PLACEHOLDER_QUOTA,
        )

    def update_mailbox(
        self,
        email: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> MailboxInfo:
        logger.debug("NullProvider.update_mailbox(%s)", email)
        return self.get_mailbox(email)

    def delete_mailbox(self, email: str) -> OperationResult:
        logger.debug("NullProvider.delete_mailbox(%s)", email)
        return _ok(f"Mailbox {email} deleted (null).")

    def list_mailboxes(self, domain: str) -> list[MailboxInfo]:
        logger.debug("NullProvider.list_mailboxes(%s)", domain)
        return []

    def set_mailbox_active(self, email: str, *, active: bool) -> OperationResult:
        logger.debug("NullProvider.set_mailbox_active(%s, %s)", email, active)
        return _ok()

    # ── Password management ───────────────────────────────────────────────

    def change_password(self, email: str, new_password: str) -> OperationResult:
        logger.debug("NullProvider.change_password(%s)", email)
        return _ok()

    # ── Quota management ──────────────────────────────────────────────────

    def get_quota(self, email: str) -> QuotaInfo:
        logger.debug("NullProvider.get_quota(%s)", email)
        return _PLACEHOLDER_QUOTA

    def set_quota(self, email: str, quota_mb: int) -> OperationResult:
        logger.debug("NullProvider.set_quota(%s, %d MB)", email, quota_mb)
        return _ok()

    # ── Alias lifecycle ───────────────────────────────────────────────────

    def create_alias(
        self,
        address: str,
        targets: list[str],
        *,
        description: str = "",
    ) -> AliasInfo:
        logger.debug("NullProvider.create_alias(%s → %s)", address, targets)
        return AliasInfo(address=address, targets=targets, description=description)

    def get_alias(self, address: str) -> AliasInfo:
        logger.debug("NullProvider.get_alias(%s)", address)
        return AliasInfo(address=address)

    def update_alias(
        self,
        address: str,
        targets: list[str],
        *,
        description: str | None = None,
    ) -> AliasInfo:
        logger.debug("NullProvider.update_alias(%s)", address)
        return AliasInfo(address=address, targets=targets)

    def delete_alias(self, address: str) -> OperationResult:
        logger.debug("NullProvider.delete_alias(%s)", address)
        return _ok(f"Alias {address} deleted (null).")

    def list_aliases(self, domain: str) -> list[AliasInfo]:
        logger.debug("NullProvider.list_aliases(%s)", domain)
        return []


# ── Send provider (for message delivery) ──────────────────────────────────────


class NullSendProvider:
    """No-op send provider for local dev and CI.

    All send operations succeed silently without actually delivering anything.
    """

    def send(self, message) -> OperationResult:
        """Silently succeed on any send request."""
        from apps.email.types import SendResult

        logger.debug(
            "NullSendProvider.send() to %s (silently discarded)", message.to_email
        )
        return SendResult(success=True, provider_message_id=f"null-{id(message)}")  # type: ignore[return-value]

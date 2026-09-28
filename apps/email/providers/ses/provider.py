"""AWS SES mail infrastructure provider — manages domain identities and DKIM.

This provider uses AWS SES EmailIdentity API to manage domain verification and
DKIM setup. Unlike providers that use TXT records, SES Easy DKIM uses CNAME
records of the form:

    {token}._domainkey.{domain}  CNAME  {token}.dkim.amazonses.com
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from apps.email.exceptions import EmailProviderError
from apps.email.types import (
    SES_MAIL_FROM_SPF,
    DkimRecord,
    DomainInfo,
    DomainStatus,
    MailFromInfo,
    OperationResult,
)

from ..base import EmailProvider

if TYPE_CHECKING:
    from mypy_boto3_sesv2 import SESv2Client

logger = logging.getLogger(__name__)


class SesProvider(EmailProvider):
    """Mail provider using AWS SES email identity and Easy DKIM."""

    def __init__(self, region: str | None = None) -> None:
        """Initialize SES client with credentials from environment/IAM role.

        ``region`` may be passed by the factory to avoid a second DB round-trip
        when MailProviderSettings was already loaded to resolve the backend.
        """
        if region is None:
            from apps.core.models import MailProviderSettings

            try:
                settings = MailProviderSettings.load()
                region = settings.aws_region or os.getenv("AWS_REGION", "us-east-1")
            except Exception:
                logger.debug(
                    "Failed to load MailProviderSettings; falling back to env/defaults"
                )
                region = os.getenv("AWS_REGION", "us-east-1")

        # Kept for the MAIL FROM MX target, which is region-specific.
        self.region: str = region
        self.client: SESv2Client = boto3.client("sesv2", region_name=region)
        self._identity_cache: dict[str, dict] = {}

    def _get_email_identity(self, domain: str) -> dict:
        """Fetch and cache the SES identity response for the current task scope."""
        if domain not in self._identity_cache:
            self._identity_cache[domain] = self.client.get_email_identity(
                EmailIdentity=domain
            )
        return self._identity_cache[domain]

    # ── Domain management ──────────────────────────────────────────────────────

    def create_domain(
        self,
        domain: str,
        *,
        max_accounts: int | None = None,
        disk_quota_mb: int | None = None,
        description: str = "",
    ) -> DomainInfo:
        """Register a domain as a sending identity in SES (Easy DKIM enabled).

        ``max_accounts`` / ``disk_quota_mb`` / ``description`` are accepted for
        interface compatibility with mail-server providers and ignored — SES has
        no such concepts. The returned :class:`DomainInfo` has ``dkim=None``;
        SES DKIM tokens are not available synchronously at create time, callers
        fetch them via :meth:`get_dkim_records` once the identity exists.
        """
        try:
            # EmailIdentity must be the bare domain, not an address like
            # "noreply@domain". Address identities use email confirmation,
            # not DNS/DKIM.
            self.client.create_email_identity(
                EmailIdentity=domain,
                Tags=[{"Key": "Source", "Value": "Automator"}],
            )
            logger.info("SES domain identity created: %s", domain)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "AlreadyExistsException":
                logger.info("SES domain identity already exists: %s", domain)
            else:
                logger.exception("Failed to create SES domain identity: %s", domain)
                raise EmailProviderError(
                    f"Failed to create domain identity: {exc}"
                ) from exc
        except Exception as exc:
            logger.exception(
                "Unexpected error creating SES domain identity: %s", domain
            )
            raise EmailProviderError(str(exc)) from exc

        return DomainInfo(
            domain=domain,
            status=DomainStatus.PENDING,
            dkim=None,
            description=description,
        )

    # ── Custom MAIL FROM ───────────────────────────────────────────────────────

    def _mail_from_mx(self) -> str:
        return f"feedback-smtp.{self.region}.amazonses.com"

    def configure_mail_from(
        self, domain: str, *, subdomain: str = "bounce"
    ) -> MailFromInfo:
        """Set ``{subdomain}.{domain}`` as the identity's custom MAIL FROM.

        BehaviorOnMxFailure=USE_DEFAULT_VALUE: if the tenant's MX record is ever
        missing or wrong, SES quietly falls back to its own return path instead
        of rejecting their mail. MAIL FROM improves alignment; it must never be
        able to stop a domain from sending.
        """
        mail_from = f"{subdomain}.{domain}"
        try:
            self.client.put_email_identity_mail_from_attributes(
                EmailIdentity=domain,
                MailFromDomain=mail_from,
                BehaviorOnMxFailure="USE_DEFAULT_VALUE",
            )
            logger.info("SES MAIL FROM set: %s -> %s", domain, mail_from)
        except ClientError as exc:
            logger.exception("Failed to set SES MAIL FROM for %s", domain)
            raise EmailProviderError(f"Failed to set MAIL FROM domain: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected error setting SES MAIL FROM for %s", domain)
            raise EmailProviderError(str(exc)) from exc

        return MailFromInfo(
            mail_from_domain=mail_from,
            status="PENDING",
            behavior_on_mx_failure="USE_DEFAULT_VALUE",
            mx_value=self._mail_from_mx(),
            spf_value=SES_MAIL_FROM_SPF,
        )

    def get_mail_from(self, domain: str) -> MailFromInfo | None:
        """Read the identity's MAIL FROM state back from SES.

        None when the identity doesn't exist or has no custom MAIL FROM.
        """
        try:
            response = self._get_email_identity(domain)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NotFoundException":
                return None
            raise EmailProviderError(f"Failed to read MAIL FROM: {exc}") from exc

        attrs = response.get("MailFromAttributes") or {}
        mail_from = attrs.get("MailFromDomain") or ""
        if not mail_from:
            return None
        return MailFromInfo(
            mail_from_domain=mail_from,
            status=attrs.get("MailFromDomainStatus") or "",
            behavior_on_mx_failure=attrs.get("BehaviorOnMxFailure") or "",
            mx_value=self._mail_from_mx(),
            spf_value=SES_MAIL_FROM_SPF,
        )

    def verify_domain(self, domain: str) -> OperationResult:
        """Return success when SES reports VerificationStatus == SUCCESS."""
        try:
            response = self._get_email_identity(domain)
            # SESv2: VerificationStatus is top-level (not under Attributes).
            status = response.get("VerificationStatus")

            if status == "SUCCESS":
                return OperationResult(success=True)

            logger.info("SES domain verification status for %s: %s", domain, status)
            return OperationResult(success=False)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NotFoundException":
                logger.info("SES domain not found for verification: %s", domain)
                return OperationResult(success=False)
            logger.exception("Failed to verify SES domain: %s", domain)
            raise EmailProviderError(f"Failed to verify domain: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected error verifying SES domain: %s", domain)
            raise EmailProviderError(str(exc)) from exc

    def get_dkim_records(self, domain: str) -> list[DkimRecord]:
        try:
            response = self._get_email_identity(domain)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NotFoundException":
                logger.info("SES identity not found for domain=%s", domain)
                return []
            logger.exception("SES get_email_identity failed for domain=%s", domain)
            raise EmailProviderError(
                f"Failed to get DKIM records for {domain}: {exc}"
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected error getting DKIM records for %s", domain)
            raise EmailProviderError(str(exc)) from exc

        tokens = (response.get("DkimAttributes") or {}).get("Tokens") or []
        records: list[DkimRecord] = []
        for token in tokens:
            record_name = f"{token}._domainkey.{domain}"
            cname_value = f"{token}.dkim.amazonses.com"
            records.append(
                DkimRecord(
                    selector=token,
                    algorithm="rsa-sha256",
                    public_key_txt=cname_value,
                    record_name=record_name,
                )
            )
            logger.debug(
                "SES DKIM CNAME for %s: %s -> %s", domain, record_name, cname_value
            )
        return records

    def get_dkim(self, domain: str, selector: str | None = None) -> DkimRecord | None:  # type: ignore[override]
        """Compatibility helper — prefer get_dkim_records() and publish all three.

        ``selector`` is ignored for Easy DKIM (SES assigns the tokens).
        """
        if selector is not None:
            logger.warning(
                "SesProvider.get_dkim(selector=...) is ignored; "
                "Easy DKIM selectors are SES-assigned tokens. "
                "Use get_dkim_records() and publish all three CNAMEs."
            )
        records = self.get_dkim_records(domain)
        if not records:
            return None
        if len(records) > 1:
            logger.warning(
                "SesProvider.get_dkim() returning only the first of %d DKIM "
                "records for domain=%s; callers must use get_dkim_records()",
                len(records),
                domain,
            )
        return records[0]

    def delete_domain(self, domain: str) -> OperationResult:
        """Delete an email identity from SES (idempotent if already gone)."""
        try:
            self.client.delete_email_identity(EmailIdentity=domain)
            logger.info("SES domain identity deleted: %s", domain)
            return OperationResult(success=True)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NotFoundException":
                logger.info("SES domain not found (already deleted): %s", domain)
                return OperationResult(success=True)
            logger.exception("Failed to delete SES domain: %s", domain)
            raise EmailProviderError(f"Failed to delete domain: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected error deleting SES domain: %s", domain)
            raise EmailProviderError(str(exc)) from exc

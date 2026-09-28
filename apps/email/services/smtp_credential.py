"""SMTP relay credential provisioning business logic.

SmtpCredentialService owns:
  - Creating a dedicated SMTP AUTH identity for a verified domain
  - Rotating / revoking that identity (no provider calls — now SES-backed)
  - Audit log writes

Credentials are stored locally and validated by a separate SMTP listener
that hands off to SES for delivery.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import TYPE_CHECKING

from apps.email.audit import record as audit
from apps.email.models import EmailDomain, SmtpCredential

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

logger = logging.getLogger(__name__)


def _generate_secret() -> str:
    return secrets.token_urlsafe(24)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class SmtpCredentialService:
    """Manages SMTP relay credentials — no longer tied to mail provider."""

    def __init__(self, account, *, actor: AbstractBaseUser | None = None) -> None:
        self.account = account
        self.actor = actor

    def provision(self, domain: EmailDomain) -> tuple[SmtpCredential, str]:
        """Create a dedicated SMTP relay identity for a verified domain.

        Returns (credential, plaintext_secret) — the plaintext is shown once
        and never persisted.
        """
        username = f"relay@{domain.domain}"
        secret = _generate_secret()

        credential = SmtpCredential.objects.create(
            account=self.account,
            domain=domain,
            username=username,
            secret_hash=_hash(secret),
            last4=secret[-4:],
        )
        audit(
            account=self.account,
            actor=self.actor,
            action="smtp_credential.provision",
            resource_type="smtp_credential",
            resource_id=username,
        )
        logger.info(
            "SmtpCredentialService.provision: %s created (account=%s)",
            username,
            self.account.pk,
        )
        return credential, secret

    def rotate(self, credential: SmtpCredential) -> str:
        """Generate a new secret for an existing relay identity. Returns the plaintext once."""
        secret = _generate_secret()

        credential.secret_hash = _hash(secret)
        credential.last4 = secret[-4:]
        credential.save(update_fields=["secret_hash", "last4"])
        audit(
            account=self.account,
            actor=self.actor,
            action="smtp_credential.rotate",
            resource_type="smtp_credential",
            resource_id=credential.username,
        )
        return secret

    def revoke(self, credential: SmtpCredential) -> None:
        """Permanently disable a relay identity."""
        credential.is_active = False
        credential.save(update_fields=["is_active"])
        audit(
            account=self.account,
            actor=self.actor,
            action="smtp_credential.revoke",
            resource_type="smtp_credential",
            resource_id=credential.username,
        )

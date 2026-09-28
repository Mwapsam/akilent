"""Alias provisioning business logic.

AliasService owns:
  - Creating, updating, and deleting forwarding aliases
  - Keeping EmailAlias (DB) in sync with the mail provider
  - Audit log writes

Views import this service — never the provider directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apps.email.audit import record as audit
from apps.email.exceptions import EmailProviderError
from apps.email.providers import get_mail_provider
from apps.email.types import AliasInfo, OperationResult

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

    class EmailAlias:  # dropped in migration 0016; service kept for reference
        address: str
        goto: str


logger = logging.getLogger(__name__)


class AliasService:
    """Orchestrates alias CRUD between Django and the mail provider."""

    def __init__(self, account, *, actor: AbstractBaseUser | None = None) -> None:
        self.account = account
        self.actor = actor
        self._provider = get_mail_provider()

    def provision(self, alias: EmailAlias) -> AliasInfo:
        """Create the alias on the mail server."""
        try:
            info = self._provider.create_alias(alias.address, [alias.goto])
        except EmailProviderError:
            audit(
                account=self.account,
                actor=self.actor,
                action="alias.provision",
                resource_type="alias",
                resource_id=alias.address,
                success=False,
                error="Provider error during create_alias",
            )
            raise

        audit(
            account=self.account,
            actor=self.actor,
            action="alias.provision",
            resource_type="alias",
            resource_id=alias.address,
            metadata={"goto": alias.goto},
        )
        logger.info(
            "AliasService.provision: %s → %s (account=%s)",
            alias.address,
            alias.goto,
            self.account.pk,
        )
        return info

    def deprovision(self, alias: EmailAlias) -> OperationResult:
        """Remove the alias from the mail server."""
        try:
            result = self._provider.delete_alias(alias.address)
        except EmailProviderError:
            audit(
                account=self.account,
                actor=self.actor,
                action="alias.deprovision",
                resource_type="alias",
                resource_id=alias.address,
                success=False,
                error="Provider error during delete_alias",
            )
            raise

        audit(
            account=self.account,
            actor=self.actor,
            action="alias.deprovision",
            resource_type="alias",
            resource_id=alias.address,
        )
        return result

    def update_targets(self, alias: EmailAlias, targets: list[str]) -> AliasInfo:
        """Replace the forwarding targets for an existing alias."""
        try:
            info = self._provider.update_alias(alias.address, targets)
        except EmailProviderError:
            audit(
                account=self.account,
                actor=self.actor,
                action="alias.update_targets",
                resource_type="alias",
                resource_id=alias.address,
                success=False,
                error="Provider error during update_alias",
            )
            raise

        audit(
            account=self.account,
            actor=self.actor,
            action="alias.update_targets",
            resource_type="alias",
            resource_id=alias.address,
            metadata={"targets": targets},
        )
        return info

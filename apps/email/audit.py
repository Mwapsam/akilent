"""Audit logging for email provisioning operations.

Every create/update/delete operation that touches the mail provider is recorded
in an AuditLog row so operators can trace who did what, when, and whether the
operation succeeded. The record() helper is the single call site throughout
the services layer.

Best-effort: a DB write failure logs an error but never raises, so an audit
failure never blocks the operation being audited.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser

logger = logging.getLogger(__name__)


def record(
    *,
    account,
    action: str,
    resource_type: str,
    resource_id: str,
    actor: AbstractBaseUser | None = None,
    success: bool = True,
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write one AuditLog entry.

    Args:
        account:        The Account being acted upon.
        action:         Dot-namespaced string, e.g. "domain.create", "mailbox.delete".
        resource_type:  "domain", "mailbox", "alias".
        resource_id:    The domain name, email address, or alias address.
        actor:          The authenticated User who triggered the action (None for system/Celery).
        success:        Whether the operation succeeded.
        error:          Error message if success=False.
        metadata:       Extra key/value pairs (quota_mb, selector, etc.).
    """
    from apps.email.models import AuditLog

    try:
        AuditLog.objects.create(
            account=account,
            actor=actor,  # type: ignore[misc]
            action=action,
            resource_type=resource_type,
            resource_id=(resource_id or "")[:255],
            success=success,
            error=(error or "")[:2000],
            metadata=metadata or {},
        )
    except Exception as exc:
        logger.error(
            "audit: failed to record action=%s resource=%s/%s success=%s: %s",
            action,
            resource_type,
            resource_id,
            success,
            exc,
        )

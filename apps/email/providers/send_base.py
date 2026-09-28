"""Provider-agnostic interface for delivering a single rendered email.

Distinct from EmailProvider (apps.email.providers.base), which manages mail
server *infrastructure* (domains, mailboxes, DKIM). This interface is only
about handing one already-rendered message to a transport — SMTP today,
optionally SendGrid/Mailgun/SES/Resend/etc. later — without call sites (Celery
tasks, services) needing to know which.

Design principle, matching EmailProvider: methods return typed dataclasses
from apps.email.types, never raw dicts or provider SDK objects.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from apps.email.types import OutboundEmail, SendResult


class EmailSendProvider(ABC):
    """Abstract interface for delivering one OutboundEmail.

    Implementations should raise EmailProviderError on unrecoverable failure
    (caller marks the EmailMessage failed / retries) rather than returning
    success=False for exceptional cases — SendResult(success=False) is for
    provider-reported soft failures that don't warrant a Python exception.
    """

    @abstractmethod
    def send(self, message: OutboundEmail) -> SendResult:
        """Deliver `message`. Raises EmailProviderError on failure."""

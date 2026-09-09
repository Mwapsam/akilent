"""Mail provider factory.

Resolves the configured backend at runtime via the MailProviderSettings singleton
(edited by superadmins via the dashboard) or falls back to the MAIL_PROVIDER_BACKEND
Django setting. Supports built-in short aliases and full dotted import paths
so any EmailProvider implementation can be plugged in without changing this file.

Built-in aliases:
    stalwart  — Stalwart Mail Server HTTP Management API (production default)
    ses       — AWS Simple Email Service
    null      — no-op stub that succeeds silently (local dev / CI)

Custom providers — set MAIL_PROVIDER_BACKEND to a full dotted import path:
    MAIL_PROVIDER_BACKEND=myapp.mail.providers.MailcowProvider
"""
import importlib
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

from .base import (
    DkimResult,
    EmailProvider,
    MailProvider,
    MailProviderError,
    ProvisionResult,
)
from .send_base import EmailSendProvider
from apps.email.exceptions import EmailProviderError
from apps.email.types import (
    AliasInfo,
    DkimRecord,
    DomainInfo,
    MailboxInfo,
    OperationResult,
    OutboundEmail,
    QuotaInfo,
    SendResult,
)

_ALIASES: dict[str, str] = {
    "stalwart": "apps.email.providers.stalwart.StalwartProvider",
    "ses":      "apps.email.providers.ses.SesProvider",
    "null":     "apps.email.providers.null.NullProvider",
}

_SEND_ALIASES: dict[str, str] = {
    "smtp": "apps.email.providers.smtp.SmtpSendProvider",
    "ses":  "apps.email.providers.ses.SesSendProvider",
    "null": "apps.email.providers.null.NullSendProvider",
    "sandbox": "apps.email.providers.sandbox.SandboxSendProvider",
}


def get_mail_provider() -> EmailProvider:
    """Return an instance of the configured mail infrastructure provider.

    Reads from MailProviderSettings singleton (edited by superadmins via dashboard),
    falling back to MAIL_PROVIDER_BACKEND env var / Django setting.

    Backend argument accepts either a short alias ("stalwart", "ses", "null") or a
    full dotted import path ("myapp.mail.MyProvider"), so any class that implements
    EmailProvider can be plugged in without touching this file or any call site.
    """
    backend: str | None = None
    try:
        from apps.core.models import MailProviderSettings
        settings_obj = MailProviderSettings.load()
        backend = settings_obj.infra_backend
        logger.debug(f"Using mail provider backend from DB: {backend}")
    except Exception as e:
        # Fall back to env var if DB read fails (e.g., migrations not yet run)
        logger.debug(f"Could not read MailProviderSettings from DB ({e}), falling back to env var")
        backend = getattr(settings, "MAIL_PROVIDER_BACKEND", "stalwart")

    dotted = _ALIASES.get(backend, backend)
    if "." not in dotted:
        raise ValueError(
            f"Mail provider backend {backend!r} is not a known alias and is not "
            "a dotted import path (e.g. 'myapp.mail.MyProvider')."
        )
    module_path, class_name = dotted.rsplit(".", 1)
    try:
        module = importlib.import_module(module_path)
        cls: type[EmailProvider] = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"Cannot load mail provider {dotted!r}: {exc}"
        ) from exc
    return cls()


def get_send_provider(mode: str = "live") -> EmailSendProvider:
    """Return an instance of the configured outbound-message send provider.

    ``mode="test"`` forces the sandbox provider regardless of configuration —
    test API keys must never touch a real transport. Otherwise reads from the
    MailProviderSettings singleton, falling back to EMAIL_SEND_PROVIDER_BACKEND.
    """
    if mode == "test":
        from apps.email.providers.sandbox import SandboxSendProvider

        return SandboxSendProvider()

    backend: str | None = None
    try:
        from apps.core.models import MailProviderSettings
        settings_obj = MailProviderSettings.load()
        backend = settings_obj.send_backend
        logger.debug(f"Using send provider backend from DB: {backend}")
    except Exception as e:
        # Fall back to env var if DB read fails (e.g., migrations not yet run)
        logger.debug(f"Could not read MailProviderSettings from DB ({e}), falling back to env var")
        backend = getattr(settings, "EMAIL_SEND_PROVIDER_BACKEND", "smtp")

    dotted = _SEND_ALIASES.get(backend, backend)
    if "." not in dotted:
        raise ValueError(
            f"Send provider backend {backend!r} is not a known alias and is "
            "not a dotted import path (e.g. 'myapp.mail.MySendProvider')."
        )
    module_path, class_name = dotted.rsplit(".", 1)
    try:
        module = importlib.import_module(module_path)
        cls: type[EmailSendProvider] = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"Cannot load send provider {dotted!r}: {exc}"
        ) from exc
    return cls()


__all__ = [
    # Factory
    "get_mail_provider",
    "get_send_provider",
    # Abstract interface
    "EmailProvider",
    "MailProvider",          # backwards-compat alias
    "EmailSendProvider",
    # Exceptions
    "EmailProviderError",
    "MailProviderError",     # backwards-compat alias
    # Legacy result shims
    "ProvisionResult",
    "DkimResult",
    # Normalized types
    "DomainInfo",
    "MailboxInfo",
    "AliasInfo",
    "DkimRecord",
    "QuotaInfo",
    "OperationResult",
    "OutboundEmail",
    "SendResult",
]

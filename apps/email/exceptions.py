"""Typed exception hierarchy for the email provider abstraction layer.

All exceptions that escape the provider layer are subclasses of
EmailProviderError. Services, views, and Celery tasks import only from here —
never from provider-specific modules.
"""
from __future__ import annotations


class EmailProviderError(Exception):
    """Base class for all mail provider failures."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, msg={str(self)!r})"


class ConfigurationError(EmailProviderError):
    """Required provider credentials or settings are missing or invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="configuration_error")


class AuthenticationError(EmailProviderError):
    """Provider rejected admin credentials or the cached token expired."""

    def __init__(self, message: str = "Authentication failed") -> None:
        super().__init__(message, code="auth_error")


class ResourceNotFoundError(EmailProviderError):
    """The requested resource does not exist on the mail server."""

    def __init__(self, resource_type: str, identifier: str) -> None:
        super().__init__(
            f"{resource_type} {identifier!r} not found",
            code="not_found",
            details={"resource_type": resource_type, "identifier": identifier},
        )
        self.resource_type = resource_type
        self.identifier = identifier


class ResourceConflictError(EmailProviderError):
    """The resource already exists (duplicate domain, mailbox, or alias)."""

    def __init__(self, resource_type: str, identifier: str) -> None:
        super().__init__(
            f"{resource_type} {identifier!r} already exists",
            code="conflict",
            details={"resource_type": resource_type, "identifier": identifier},
        )
        self.resource_type = resource_type
        self.identifier = identifier


class ProvisioningError(EmailProviderError):
    """A provisioning operation failed mid-way (partial state is possible)."""

    def __init__(
        self,
        message: str,
        *,
        resource: str = "",
        operation: str = "",
    ) -> None:
        super().__init__(
            message,
            code="provisioning_error",
            details={"resource": resource, "operation": operation},
        )
        self.resource = resource
        self.operation = operation


class RateLimitError(EmailProviderError):
    """The provider is rate-limiting outgoing management API requests."""

    def __init__(self, retry_after: int | None = None) -> None:
        msg = "Rate limit exceeded"
        if retry_after:
            msg += f" — retry after {retry_after}s"
        super().__init__(msg, code="rate_limit", details={"retry_after": retry_after})
        self.retry_after = retry_after


class ProviderTimeoutError(EmailProviderError):
    """A request to the mail provider timed out."""

    def __init__(self, operation: str = "") -> None:
        super().__init__(
            f"Provider request timed out{f': {operation}' if operation else ''}",
            code="timeout",
        )


class ValidationError(EmailProviderError):
    """The provider rejected input (bad address format, invalid quota, etc.)."""

    def __init__(self, message: str, *, field: str = "") -> None:
        super().__init__(message, code="validation_error", details={"field": field})
        self.field = field


# Backwards-compatible alias — existing code that catches MailProviderError
# (from apps.email.providers.base) works without a mass import change.
MailProviderError = EmailProviderError


class UnverifiedDomainError(Exception):
    """The `from` address's domain isn't a verified sending domain for the account."""

    def __init__(self, domain: str):
        self.domain = domain
        super().__init__(
            f"'{domain}' is not a verified sending domain for this account"
        )


class MissingPostalAddressError(Exception):
    """The account has no mailing address, so its campaigns can't be CAN-SPAM compliant.

    Every marketing email must carry the sender's physical postal address, which
    is rendered from the Account business profile (see
    apps.email.services.compliance_footer).
    """

    def __init__(self) -> None:
        super().__init__(
            "A physical mailing address is required before sending campaigns. "
            "Add one under Settings → Business."
        )

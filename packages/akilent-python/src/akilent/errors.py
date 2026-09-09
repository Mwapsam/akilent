"""Typed errors mirroring the API's ``{"error": {...}}`` envelope."""
from __future__ import annotations

from typing import Any, Optional


class AkilentError(Exception):
    """Base class for every error raised by the SDK."""


class APIConnectionError(AkilentError):
    """The request never reached the API (DNS, TLS, timeout, connection reset)."""


class APIStatusError(AkilentError):
    """The API returned a non-2xx response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: Optional[str] = None,
        request_id: Optional[str] = None,
        docs_url: Optional[str] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.docs_url = docs_url
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        base = super().__str__()
        extra = f" (code={self.code}, request_id={self.request_id})" if self.code else ""
        return base + extra


class AuthenticationError(APIStatusError):
    """401 — the API key is missing or invalid."""


class PermissionError_(APIStatusError):
    """403 — the key lacks a feature, scope, or the domain is unverified."""


class NotFoundError(APIStatusError):
    """404 — the resource does not exist."""


class ConflictError(APIStatusError):
    """409 — idempotency-key reuse or in-progress."""


class RateLimitError(APIStatusError):
    """429 — too many requests. Inspect ``retry_after``."""

    def __init__(self, *args: Any, retry_after: Optional[int] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retry_after = retry_after


class ValidationError(APIStatusError):
    """400 — the request body failed validation."""


class ServerError(APIStatusError):
    """5xx — an error on Akilent's side."""


_STATUS_MAP = {
    400: ValidationError,
    401: AuthenticationError,
    403: PermissionError_,
    404: NotFoundError,
    409: ConflictError,
    429: RateLimitError,
}


def error_from_response(status_code: int, body: Any, headers: Any) -> APIStatusError:
    err = (body or {}).get("error", {}) if isinstance(body, dict) else {}
    message = err.get("message") or f"HTTP {status_code}"
    cls = _STATUS_MAP.get(status_code)
    if cls is None:
        cls = ServerError if status_code >= 500 else APIStatusError
    kwargs = dict(
        status_code=status_code,
        code=err.get("code"),
        request_id=err.get("request_id") or headers.get("x-request-id"),
        docs_url=err.get("docs_url"),
        body=body,
    )
    if cls is RateLimitError:
        retry = headers.get("retry-after")
        kwargs["retry_after"] = int(retry) if retry and str(retry).isdigit() else None
    return cls(message, **kwargs)

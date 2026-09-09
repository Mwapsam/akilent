"""Catalog of stable public API error codes.

Every ``{"error": {"code": ...}}`` the API returns should have an entry here so
the value is documented, greppable, and stable across releases. ``errors.py``
looks codes up here to attach a ``docs_url``.
"""
from __future__ import annotations

_DOCS_BASE = "https://akilent.com/docs/errors"


class ErrorCode:
    # auth / permission
    AUTHENTICATION_FAILED = "authentication_failed"
    NOT_AUTHENTICATED = "not_authenticated"
    PERMISSION_DENIED = "permission_denied"
    MISSING_SCOPE = "missing_scope"

    # request shape
    VALIDATION_ERROR = "validation_error"
    PARSE_ERROR = "parse_error"
    MISSING_CONTENT = "missing_content"

    # domain / sending
    UNVERIFIED_DOMAIN = "unverified_domain"
    RECIPIENT_CAP_EXCEEDED = "recipient_cap_exceeded"
    SUPPRESSED_RECIPIENT = "suppressed_recipient"
    REPUTATION_HALT = "reputation_halt"

    # plan limits
    PLAN_LIMIT_EXCEEDED = "plan_limit_exceeded"
    FEATURE_NOT_AVAILABLE = "feature_not_available"

    # idempotency
    IDEMPOTENCY_KEY_REUSE = "idempotency_key_reuse"
    IDEMPOTENCY_KEY_IN_PROGRESS = "idempotency_key_in_progress"

    # generic
    NOT_FOUND = "not_found"
    THROTTLED = "throttled"
    SERVER_ERROR = "server_error"


CATALOG: dict[str, dict[str, str]] = {
    ErrorCode.AUTHENTICATION_FAILED: {"message": "The API key is missing or invalid.", "http": "401"},
    ErrorCode.NOT_AUTHENTICATED: {"message": "Authentication credentials were not provided.", "http": "401"},
    ErrorCode.PERMISSION_DENIED: {"message": "This key is not permitted to perform this action.", "http": "403"},
    ErrorCode.MISSING_SCOPE: {"message": "This key is missing a required scope.", "http": "403"},
    ErrorCode.VALIDATION_ERROR: {"message": "One or more fields failed validation.", "http": "400"},
    ErrorCode.PARSE_ERROR: {"message": "The request body could not be parsed.", "http": "400"},
    ErrorCode.MISSING_CONTENT: {"message": "The message or template has no renderable content.", "http": "400"},
    ErrorCode.UNVERIFIED_DOMAIN: {"message": "The sending domain is not verified for this account.", "http": "403"},
    ErrorCode.RECIPIENT_CAP_EXCEEDED: {"message": "The campaign exceeds your per-campaign recipient cap.", "http": "403"},
    ErrorCode.SUPPRESSED_RECIPIENT: {"message": "The recipient is on your suppression list.", "http": "403"},
    ErrorCode.REPUTATION_HALT: {"message": "Sending is paused for this account due to a reputation halt.", "http": "403"},
    ErrorCode.PLAN_LIMIT_EXCEEDED: {"message": "A plan limit was reached.", "http": "403"},
    ErrorCode.FEATURE_NOT_AVAILABLE: {"message": "Your plan does not include this feature.", "http": "403"},
    ErrorCode.IDEMPOTENCY_KEY_REUSE: {"message": "This Idempotency-Key was used with a different request body.", "http": "409"},
    ErrorCode.IDEMPOTENCY_KEY_IN_PROGRESS: {"message": "A request with this Idempotency-Key is still being processed.", "http": "409"},
    ErrorCode.NOT_FOUND: {"message": "The requested resource does not exist.", "http": "404"},
    ErrorCode.THROTTLED: {"message": "Rate limit exceeded; retry after the indicated delay.", "http": "429"},
    ErrorCode.SERVER_ERROR: {"message": "An unexpected error occurred.", "http": "500"},
}


def docs_url(code: str) -> str:
    return f"{_DOCS_BASE}#{code}"


def describe(code: str) -> dict[str, str] | None:
    entry = CATALOG.get(code)
    if entry is None:
        return None
    return {**entry, "code": code, "docs_url": docs_url(code)}

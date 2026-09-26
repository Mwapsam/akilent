"""API-key authentication for the public Developer Platform (apps.api).

Separate from the session-cookie auth used by the dashboard — this is the
only authentication class wired into apps.api views.
"""
from __future__ import annotations

from django.core.cache import cache
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication

from apps.email.models import EmailApiKey

_LOCKOUT_MAX_ATTEMPTS = 10
_LOCKOUT_WINDOW_SECONDS = 300  # 5 minutes
_LOCKOUT_CACHE_PREFIX = "apikey_bad_attempts"


def _client_ip(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "unknown")


def _lockout_cache_key(request) -> str:
    return f"{_LOCKOUT_CACHE_PREFIX}:{_client_ip(request)}"


def _record_failed_attempt(request) -> int:
    key = _lockout_cache_key(request)
    attempts = cache.get(key, 0) + 1
    cache.set(key, attempts, timeout=_LOCKOUT_WINDOW_SECONDS)
    return attempts


def _is_locked_out(request) -> bool:
    return cache.get(_lockout_cache_key(request), 0) >= _LOCKOUT_MAX_ATTEMPTS


class EmailApiKeyAuthentication(BaseAuthentication):
    """Authenticates ``X-Api-Key`` / ``Authorization: Bearer`` against EmailApiKey.

    Returns ``(account, api_key)`` on success, mirroring DRF's
    ``(user, auth)`` convention — views read ``request.auth`` for the
    ``EmailApiKey`` instance and ``request.user`` for the ``Account``
    (this API has no Django ``User`` concept; keys belong to accounts).

    Repeated bad keys from the same client IP are locked out for a window —
    the legacy hand-rolled `_authenticate()` in apps.email.views had no such
    protection, letting a caller brute-force keys with unlimited attempts.
    """

    def authenticate(self, request):
        if _is_locked_out(request):
            raise exceptions.Throttled(
                detail="Too many invalid API key attempts. Try again later."
            )

        header = request.headers.get("X-Api-Key") or ""
        if not header:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                header = auth.removeprefix("Bearer ").strip()
        if not header:
            # This is the only authentication class on these views, so a
            # missing key gets the same 401 as an invalid one — rather than
            # DRF's usual "return None to try the next authenticator" idiom,
            # which would let the request fall through to a 403 instead.
            raise exceptions.AuthenticationFailed("Invalid or missing API key")

        api_key = EmailApiKey.authenticate(header)
        if api_key is None:
            _record_failed_attempt(request)
            raise exceptions.AuthenticationFailed("Invalid or missing API key")
        if not api_key.account.is_active:
            raise exceptions.AuthenticationFailed("This account is suspended. Contact support.")

        return (api_key.account, api_key)

    def authenticate_header(self, request):
        return "Bearer"

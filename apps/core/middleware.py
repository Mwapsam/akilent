"""Cross-cutting HTTP middleware."""

from __future__ import annotations

import hashlib

from apps.core.browser_session import (
    reset_browser_session_id,
    set_browser_session_id,
)
from apps.core.request_context import (
    new_request_id,
    reset_request_id,
    set_request_id,
)

# ---------------------------------------------------------------------------
# Security-response headers
# ---------------------------------------------------------------------------
# Tightened where possible without breaking HTMX / Alpine / whitenoise:
#   * CSP allows 'unsafe-inline' (inline theme-toggle script in base.html)
#     and 'unsafe-eval' (Alpine 3 uses Function() to evaluate x-data expressions).
#   * img-src: uses specific S3/CloudFront domain in production; falls back to
#     https: in dev (no S3). Avoids the wildcard-directive ZAP warning in prod.
#   * connect-src: 'self' only — SSE uses HTTP EventSource, not WebSocket.
#   * script-src: adds googletagmanager.com when GA is configured.
#   * COEP is intentionally omitted: Cloudflare cdn-cgi resources lack CORP
#     headers, so require-corp would break error pages served from that origin.

_csp_cache: str | None = None


def _build_csp() -> str:
    from django.conf import settings

    # Static assets (JS, CSS, fonts, images) are served from the S3/CloudFront domain.
    # All four directives need it; fall back to https: in dev where S3 is not configured.
    custom_domain = getattr(settings, "AWS_S3_CUSTOM_DOMAIN", "")
    cdn = f"https://{custom_domain}" if custom_domain else "https:"

    # script-src: allow Google Analytics loader only when a GA ID is configured.
    # Cloudflare Insights injects beacon.min.js from static.cloudflareinsights.com.
    ga_id = getattr(settings, "GOOGLE_ANALYTICS_ID", "")
    script_src_extra = " https://www.googletagmanager.com" if ga_id else ""
    # connect-src: GA4 beacons go to analytics.google.com and www.google.com.
    connect_src_extra = (
        " https://analytics.google.com https://www.google.com" if ga_id else ""
    )

    return (
        "default-src 'self'; "
        f"script-src 'self' 'unsafe-inline' 'unsafe-eval' {cdn}"
        f" https://static.cloudflareinsights.com{script_src_extra}; "
        f"style-src 'self' 'unsafe-inline' {cdn}; "
        f"img-src 'self' data: blob: {cdn}; "
        f"font-src 'self' data: {cdn}; "
        f"connect-src 'self'{connect_src_extra}; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none';"
    )


def _get_csp() -> str:
    global _csp_cache
    if _csp_cache is None:
        _csp_cache = _build_csp()
    return _csp_cache


_PERMISSIONS = (
    "accelerometer=(), camera=(), display-capture=(), geolocation=(), "
    "gyroscope=(), magnetometer=(), microphone=(), payment=(), usb=()"
)

_INBOUND_HEADER = "HTTP_X_REQUEST_ID"
_RESPONSE_HEADER = "X-Request-Id"

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Paths that keep working for a suspended member or while viewing as a business.
_ALWAYS_OPEN = (
    "/auth/logout/",
    "/static/",
    "/media/",
    "/healthz",
    "/privacy/",
    "/terms/",
    "/data-deletion/",
    "/help/",
    "/docs/",
    "/manage/",
    "/api/",
)


def _open(path: str) -> bool:
    return path.startswith(_ALWAYS_OPEN)


def _wants_json(request) -> bool:
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or (
            request.headers.get("hx-request") == "true"
            and request.headers.get("hx-boosted") != "true"
        )
        or "application/json" in request.headers.get("accept", "")
    )


class SecurityHeadersMiddleware:
    """Emit security headers that Django's SecurityMiddleware does not cover."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", _get_csp())
        response.setdefault("Permissions-Policy", _PERMISSIONS)
        response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response


def _parse_browser(ua: str) -> str:
    ua = ua.lower()
    for name in ("edg/", "opr/", "chrome/", "firefox/", "safari/"):
        if name in ua:
            return (
                name.rstrip("/").replace("edg", "edge").replace("opr", "opera").title()
            )
    return "unknown"


def _parse_device(ua: str) -> str:
    ua = ua.lower()
    if "mobile" in ua or "android" in ua and "mobi" in ua:
        return "mobile"
    if "tablet" in ua or "ipad" in ua:
        return "tablet"
    return "desktop"


_BS_INBOUND = "HTTP_X_BROWSER_SESSION_ID"
_BS_COOKIE = "bsid"
_BS_MAX_AGE = 4 * 60 * 60  # 4 hours


class BrowserSessionMiddleware:
    """Resolve a pseudonymous browser session ID from the inbound header or
    cookie, set ``request.browser_session_id``, establish the ContextVar, and
    upsert the ``BrowserSession`` row.

    Must be placed after ``RequestIdMiddleware`` in MIDDLEWARE.  Never reaches
    into ``RequestIdMiddleware``'s internals — clean responsibility boundary.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raw = request.META.get(_BS_INBOUND, "") or request.COOKIES.get(_BS_COOKIE, "")
        sid = raw[:64] if (raw and raw.startswith("sess_")) else ""
        request.browser_session_id = sid

        token = set_browser_session_id(sid)
        try:
            if sid:
                _upsert_browser_session(request, sid)
            response = self.get_response(request)
        finally:
            reset_browser_session_id(token)

        if sid:
            from django.conf import settings as _s

            response.set_cookie(
                _BS_COOKIE,
                sid,
                max_age=_BS_MAX_AGE,
                secure=not _s.DEBUG,
                httponly=True,
                samesite="Lax",
            )
        return response


def _upsert_browser_session(request, sid: str) -> None:
    """Create or touch BrowserSession.  Swallows all errors — never raises
    into the request path."""
    try:
        from django.conf import settings as _s
        from django.utils import timezone

        from apps.logs.models import BrowserSession

        now = timezone.now()
        account = getattr(request, "account", None)
        user = getattr(request, "user", None)
        user_id_hash = (
            hashlib.sha256(str(user.pk).encode()).hexdigest()
            if user and getattr(user, "is_authenticated", False)
            else ""
        )
        release = getattr(_s, "RELEASE_VERSION", "")
        ua = request.META.get("HTTP_USER_AGENT", "")

        _obj, created = BrowserSession.objects.update_or_create(
            session_id=sid,
            defaults=dict(
                last_seen_at=now,
                release=release,
                browser=_parse_browser(ua),
                device=_parse_device(ua),
            ),
            create_defaults=dict(
                last_seen_at=now,
                account=account,
                user_id_hash=user_id_hash,
                release=release,
                browser=_parse_browser(ua),
                device=_parse_device(ua),
                entry_path=request.path[:255],
            ),
        )
        if not created:
            # Update account / user association as soon as authentication is known.
            if account is not None and _obj.account_id is None:
                BrowserSession.objects.filter(pk=_obj.pk).update(
                    account=account,
                    user_id_hash=user_id_hash,
                )
    except Exception:
        import logging

        logging.getLogger(__name__).debug(
            "_upsert_browser_session failed for sid=%s", sid, exc_info=True
        )


class SuspendedAccountMiddleware:
    """Members of a suspended business (``Account.is_active=False``) see one page saying so.

    ``get_current_account`` already ignores suspended workspaces; this turns the resulting
    "no workspace" into a clear explanation instead of an empty dashboard. Operators are exempt.
    The public API refuses such keys in ``EmailApiKeyAuthentication``.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from apps.accounts.utils import is_suspended_member
        from apps.core.utils import is_operator

        if (
            not _open(request.path)
            and not is_operator(request.user)
            and is_suspended_member(request)
        ):
            from django.http import JsonResponse
            from django.shortcuts import render

            if _wants_json(request):
                return JsonResponse(
                    {"error": "This workspace is suspended. Contact support."},
                    status=403,
                )
            return render(request, "accounts/suspended.html", status=403)
        return self.get_response(request)


class ViewAsReadOnlyMiddleware:
    """While an operator views a business ("View as"), nothing may change.

    Any request that could write (POST, PUT, PATCH, DELETE) outside the Operator Console is
    refused, so support can look at a business's screens without sending a message, saving a
    setting or publishing a workflow by accident. Stopping, logging out and the console stay open.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in _SAFE_METHODS and not _open(request.path):
            from apps.accounts.utils import viewing_as

            viewed = viewing_as(request)
            if viewed is not None:
                from django.http import HttpResponse, JsonResponse

                message = f"Read-only: you're viewing {viewed.company_name} as support. Nothing was changed."
                if _wants_json(request):
                    return JsonResponse({"error": message}, status=403)
                return HttpResponse(
                    message, status=403, content_type="text/plain; charset=utf-8"
                )
        return self.get_response(request)


class RequestIdMiddleware:
    """Assign every request a stable id and echo it back.

    Honours an inbound ``X-Request-Id`` (trimmed, length-capped) so a caller or
    upstream proxy can correlate; otherwise generates one. The id is stashed on
    ``request.id`` and in a contextvar for non-request code.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        inbound = (request.META.get(_INBOUND_HEADER) or "").strip()[:64]
        request_id = inbound or new_request_id()
        request.id = request_id
        token = set_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            reset_request_id(token)
        response[_RESPONSE_HEADER] = request_id
        return response

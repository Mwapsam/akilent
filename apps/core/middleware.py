"""Cross-cutting HTTP middleware."""
from __future__ import annotations

from apps.core.request_context import (
    new_request_id,
    reset_request_id,
    set_request_id,
)

_INBOUND_HEADER = "HTTP_X_REQUEST_ID"
_RESPONSE_HEADER = "X-Request-Id"


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Paths that keep working for a suspended member or while viewing as a business.
_ALWAYS_OPEN = (
    "/auth/logout/", "/static/", "/media/", "/healthz", "/privacy/", "/terms/", "/data-deletion/",
    "/help/", "/docs/", "/manage/", "/api/",
)


def _open(path: str) -> bool:
    return path.startswith(_ALWAYS_OPEN)


def _wants_json(request) -> bool:
    return (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.headers.get("hx-request") == "true"
        or "application/json" in request.headers.get("accept", "")
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

        if not _open(request.path) and not is_operator(request.user) and is_suspended_member(request):
            from django.http import JsonResponse
            from django.shortcuts import render

            if _wants_json(request):
                return JsonResponse({"error": "This workspace is suspended. Contact support."}, status=403)
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
                return HttpResponse(message, status=403, content_type="text/plain; charset=utf-8")
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

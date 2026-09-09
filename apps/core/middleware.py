"""Cross-cutting HTTP middleware."""
from __future__ import annotations

from apps.core.request_context import (
    new_request_id,
    reset_request_id,
    set_request_id,
)

_INBOUND_HEADER = "HTTP_X_REQUEST_ID"
_RESPONSE_HEADER = "X-Request-Id"


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

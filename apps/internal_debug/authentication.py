"""Single shared-secret authentication for the internal debug API.

Not for customers — this is for engineers debugging a live deployment
(originally: the MCP debug tool). Deliberately simpler than
apps.api.authentication.EmailApiKeyAuthentication: one token from settings,
constant-time compared, no per-account DB model. Combined with the
INTERNAL_DEBUG_ENABLED kill-switch in urls.py, both must be true for any of
these routes to be reachable at all.
"""
from __future__ import annotations

import hmac

from django.conf import settings
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication


class InternalDebugTokenAuthentication(BaseAuthentication):
    """Authenticates ``Authorization: Bearer <INTERNAL_DEBUG_TOKEN>``."""

    def authenticate(self, request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise exceptions.AuthenticationFailed("Invalid or missing debug token")

        token = auth.removeprefix("Bearer ").strip()
        expected = settings.INTERNAL_DEBUG_TOKEN
        if not expected or not token or not hmac.compare_digest(token, expected):
            raise exceptions.AuthenticationFailed("Invalid or missing debug token")

        # No Django User is involved; return a truthy placeholder so DRF
        # treats the request as authenticated.
        return ("internal-debug", token)

    def authenticate_header(self, request):
        return "Bearer"

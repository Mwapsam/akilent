"""Shared base view for the public API.

Centralises auth/throttle wiring and adds request logging + idempotency-key
support without each view having to opt in. Concrete views still declare their
own ``permission_classes`` / ``required_scope``.
"""
from __future__ import annotations

import logging
import time

from rest_framework.response import Response
from rest_framework.views import APIView

from apps.api.authentication import EmailApiKeyAuthentication
from apps.api.permissions import HasEmailApiFeature
from apps.api.throttling import ApiKeyRateThrottle
from apps.logs.observability import (
    IdempotencyReplay,
    idempotency_complete,
    idempotency_lookup,
    log_api_request,
)

logger = logging.getLogger(__name__)


class BaseApiView(APIView):
    authentication_classes = [EmailApiKeyAuthentication]
    permission_classes = [HasEmailApiFeature]
    throttle_classes = [ApiKeyRateThrottle]

    # Set on views that support Idempotency-Key (POST create endpoints).
    idempotency_endpoint: str | None = None

    def dispatch(self, request, *args, **kwargs):
        started = time.monotonic()
        response = super().dispatch(request, *args, **kwargs)
        latency_ms = int((time.monotonic() - started) * 1000)
        log_api_request(self.request, response, latency_ms=latency_ms, view=self)
        return response

    def handle_exception(self, exc):
        if isinstance(exc, IdempotencyReplay):
            return Response(exc.body, status=exc.status_code)
        return super().handle_exception(exc)

    # --- helpers for POST handlers -------------------------------------------------

    def begin_idempotency(self, request):
        """Return the PROCESSING record (or None). May raise IdempotencyReplay."""
        if not self.idempotency_endpoint:
            return None
        record, _ = idempotency_lookup(request, self.idempotency_endpoint)
        return record

    def finish_idempotency(self, record, response):
        idempotency_complete(record, response)
        return response

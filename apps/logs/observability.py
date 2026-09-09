"""API request logging + idempotency-key handling for ``apps.api``.

Kept out of ``services.py`` (message events) on purpose — different concern,
different consumers. All entry points are best-effort and never raise into the
API response path.
"""
from __future__ import annotations

import hashlib
import json
import logging

from django.utils import timezone

from apps.logs.models import ApiRequest, IdempotencyRecord
from apps.logs.redaction import redact_body, redact_headers

logger = logging.getLogger(__name__)


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR") or None


def _fingerprint(endpoint: str, body) -> str:
    try:
        payload = json.dumps(body, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(body)
    return hashlib.sha256(f"{endpoint}\n{payload}".encode()).hexdigest()


def log_api_request(request, response, *, latency_ms: int, view=None) -> None:
    """Persist one ApiRequest row. Swallows all errors."""
    try:
        account = getattr(request, "user", None)
        account = account if getattr(account, "pk", None) else None
        api_key = getattr(request, "auth", None)
        api_key = api_key if getattr(api_key, "pk", None) else None

        error_code = ""
        body = getattr(response, "data", None)
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error_code = str(body["error"].get("code", ""))[:64]

        ApiRequest.objects.create(
            account=account,
            api_key=api_key,
            key_mode=getattr(api_key, "mode", "") or "",
            method=request.method,
            path=request.path[:255],
            version=str(getattr(request, "version", "") or "")[:10],
            status_code=getattr(response, "status_code", 0) or 0,
            error_code=error_code,
            request_id=getattr(request, "id", "") or "",
            idempotency_key=(request.META.get("HTTP_IDEMPOTENCY_KEY") or "")[:255],
            idempotency_replayed=bool(getattr(request, "_idempotency_replayed", False)),
            latency_ms=max(0, latency_ms),
            request_headers=redact_headers(
                {k[5:].replace("_", "-"): v for k, v in request.META.items() if k.startswith("HTTP_")}
            ),
            request_body=redact_body(_safe_data(request)),
            response_body=redact_body(body if isinstance(body, (dict, list)) else {}),
            client_ip=_client_ip(request),
            user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:512],
        )
    except Exception:  # noqa: BLE001
        logger.exception("log_api_request failed")


def _safe_data(request):
    try:
        data = request.data
    except Exception:  # noqa: BLE001 - unparseable body
        return {}
    return data if isinstance(data, (dict, list)) else {}


class IdempotencyReplay(Exception):
    """Raised internally to short-circuit with a stored response."""

    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self.body = body


def idempotency_lookup(request, endpoint: str):
    """Return (record, replay_response_or_None).

    ``record`` is a fresh PROCESSING row to complete later, or ``None`` when no
    key was supplied. Raises :class:`IdempotencyReplay` for a completed match,
    and a DRF ``ValidationError``-shaped dict via return for conflicts.
    """
    key = (request.META.get("HTTP_IDEMPOTENCY_KEY") or "").strip()
    if not key:
        return None, None

    account = getattr(request, "user", None)
    if not getattr(account, "pk", None):
        return None, None

    fingerprint = _fingerprint(endpoint, _safe_data(request))
    record, created = IdempotencyRecord.objects.get_or_create(
        account=account,
        key=key[:255],
        defaults={"endpoint": endpoint[:128], "request_fingerprint": fingerprint},
    )
    if created:
        return record, None

    if record.request_fingerprint != fingerprint:
        raise IdempotencyReplay(
            409,
            {"error": {"code": "idempotency_key_reuse",
                       "message": "This Idempotency-Key was used with a different request body."}},
        )
    if record.status == IdempotencyRecord.Status.PROCESSING:
        raise IdempotencyReplay(
            409,
            {"error": {"code": "idempotency_key_in_progress",
                       "message": "A request with this Idempotency-Key is still being processed."}},
        )
    request._idempotency_replayed = True
    raise IdempotencyReplay(record.response_status or 200, record.response_body or {})


def idempotency_complete(record, response) -> None:
    if record is None:
        return
    try:
        record.status = IdempotencyRecord.Status.COMPLETED
        record.response_status = getattr(response, "status_code", 200)
        body = getattr(response, "data", None)
        record.response_body = body if isinstance(body, (dict, list)) else {}
        record.completed_at = timezone.now()
        record.save(
            update_fields=["status", "response_status", "response_body", "completed_at"]
        )
    except Exception:  # noqa: BLE001
        logger.exception("idempotency_complete failed for record %s", record.pk)

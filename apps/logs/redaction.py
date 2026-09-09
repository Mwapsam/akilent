"""Best-effort redaction for request/response snapshots stored in ApiRequest."""
from __future__ import annotations

import re

_SECRET_KEY_RE = re.compile(r"(secret|token|password|api[-_]?key|authorization)", re.I)
_HEADER_ALLOW = {
    "content-type",
    "accept",
    "user-agent",
    "idempotency-key",
    "x-request-id",
    "x-api-version",
}
_MAX_CHARS = 16_000
_REDACTED = "[redacted]"


def redact_headers(headers: dict) -> dict:
    out = {}
    for key, value in (headers or {}).items():
        lk = key.lower()
        if lk not in _HEADER_ALLOW:
            continue
        out[lk] = value
    return out


def redact_body(value):
    """Recursively blank values whose key looks secret; cap overall size."""
    redacted = _redact(value)
    return _cap(redacted)


def _redact(value):
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _SECRET_KEY_RE.search(str(k)) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _cap(value):
    try:
        import json

        text = json.dumps(value)
    except (TypeError, ValueError):
        return {"_note": "unserializable body"}
    if len(text) > _MAX_CHARS:
        return {"_truncated": True, "_preview": text[:_MAX_CHARS]}
    return value

"""HubSpot webhook signature verification.

Supports v3 (preferred) and v1 (fallback).

v3: HMAC-SHA256 of "{method}{uri}{body}{timestamp}", base64-encoded.
    Timestamp must be within 5 minutes to prevent replay attacks.

v1: HMAC-SHA256 of "{client_secret}{body}", hex-encoded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


class SignatureError(Exception):
    pass


def verify_v3(
    client_secret: str,
    method: str,
    uri: str,
    body: bytes,
    signature_b64: str,
    timestamp_ms_str: str,
    *,
    max_age_seconds: int = 300,
) -> None:
    """Raise SignatureError if the v3 signature is invalid or stale."""
    try:
        timestamp_ms = int(timestamp_ms_str)
    except (ValueError, TypeError) as exc:
        raise SignatureError("Invalid X-HubSpot-Request-Timestamp header") from exc

    now_ms = int(time.time() * 1000)
    age_seconds = abs(now_ms - timestamp_ms) / 1000
    if age_seconds > max_age_seconds:
        raise SignatureError(
            f"Request timestamp is {age_seconds:.0f}s old (max {max_age_seconds}s)"
        )

    source = f"{method}{uri}{body.decode('utf-8', errors='replace')}{timestamp_ms_str}"
    expected = base64.b64encode(
        hmac.new(client_secret.encode(), source.encode(), hashlib.sha256).digest()
    ).decode()

    if not hmac.compare_digest(expected, signature_b64):
        raise SignatureError("v3 signature mismatch")


def verify_v1(client_secret: str, body: bytes, signature_hex: str) -> None:
    """Raise SignatureError if the v1 signature is invalid."""
    source = client_secret.encode() + body
    expected = hmac.new(client_secret.encode(), source, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_hex):
        raise SignatureError("v1 signature mismatch")

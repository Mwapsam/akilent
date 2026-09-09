"""Verify inbound Akilent webhook signatures (Stripe-style scheme)."""
from __future__ import annotations

import hashlib
import hmac
import time

_TOLERANCE_SECONDS = 300


class SignatureVerificationError(Exception):
    pass


def verify(payload: bytes | str, header: str, secret: str, *, tolerance: int = _TOLERANCE_SECONDS) -> bool:
    """Return True if ``header`` (``t=<unix>,v1=<hex>``) matches ``payload``.

    Raises :class:`SignatureVerificationError` on a malformed header or a
    timestamp outside the tolerance window.
    """
    if isinstance(payload, str):
        payload = payload.encode()
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        timestamp = int(parts["t"])
        received = parts["v1"]
    except (KeyError, ValueError) as exc:
        raise SignatureVerificationError("malformed signature header") from exc

    if abs(time.time() - timestamp) > tolerance:
        raise SignatureVerificationError("timestamp outside tolerance window")

    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise SignatureVerificationError("signature mismatch")
    return True

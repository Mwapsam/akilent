"""Meta webhook signature verification, shared by the WhatsApp and Instagram webhooks.

Meta signs every webhook POST with HMAC-SHA256(app_secret, raw body) in the
``X-Hub-Signature-256`` header, using the secret of the app the subscription
belongs to. A deployment may legitimately hold more than one secret (the Meta
app secret and the separate Instagram app secret), so a POST is accepted when it
matches any configured one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

logger = logging.getLogger(__name__)


def verify_meta_signature(request, secrets) -> bool:
    """True if the request's X-Hub-Signature-256 matches any of ``secrets``."""
    header = request.headers.get("X-Hub-Signature-256", "")
    if not header.startswith("sha256="):
        return False
    received = header.removeprefix("sha256=")
    for secret in secrets:
        if not secret:
            continue
        expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(received, expected):
            return True
    return False


def log_signature_failure(request, channel: str) -> None:
    """Say enough to tell "wrong app" from "no header" without logging a secret or digest.

    ``entry[0].id`` (a WABA id or Instagram user id) names whose events these are, which is
    what shows that the deliveries come from an app whose secret this server doesn't hold.
    """
    entry_id = ""
    try:
        entry_id = str((json.loads(request.body).get("entry") or [{}])[0].get("id", ""))
    except (ValueError, AttributeError, IndexError, TypeError):
        pass
    logger.error(
        "%s webhook: signature mismatch (header_present=%s entry_id=%s user_agent=%s remote=%s) "
        "— the sending Meta app's secret is not configured on this server",
        channel,
        bool(request.headers.get("X-Hub-Signature-256")),
        entry_id or "?",
        request.headers.get("User-Agent", ""),
        request.META.get("REMOTE_ADDR"),
    )

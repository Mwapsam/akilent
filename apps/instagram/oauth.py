"""Instagram Business Login OAuth helpers.

Uses Meta's Instagram Login for Business API:
  Auth dialog:    https://www.instagram.com/oauth/authorize
  Token exchange: https://api.instagram.com/oauth/access_token  (POST, short-lived)
  Long-lived:     https://graph.instagram.com/access_token      (GET with query params, 60 days)
  Account info:   https://graph.instagram.com/me

This is distinct from the Facebook Login flow used by WhatsApp.  The token
returned here is scoped directly to the Instagram Business Account — no Facebook
Page intermediary is required.
"""

import base64
import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH_IG = "https://graph.instagram.com"
API_IG = "https://api.instagram.com"
_TIMEOUT = 15

# Only what Akilent uses — App Review rejects permissions the product doesn't need.
SCOPES = (
    "instagram_business_basic,"
    "instagram_business_manage_messages,"
    "instagram_business_manage_comments"
)


class InstagramOAuthError(Exception):
    pass


def exchange_code_for_token(code: str, redirect_uri: str) -> str:
    """Exchange an OAuth authorization code for a short-lived Instagram access token.

    Meta requires a POST (not GET) to api.instagram.com for this step.
    ``redirect_uri`` must be byte-identical to the one used in the auth dialog.
    """
    if not settings.INSTAGRAM_APP_ID or not settings.INSTAGRAM_APP_SECRET:
        raise InstagramOAuthError(
            "INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET must be configured."
        )
    resp = requests.post(
        f"{API_IG}/oauth/access_token",
        data={
            "client_id": settings.INSTAGRAM_APP_ID,
            "client_secret": settings.INSTAGRAM_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=_TIMEOUT,
        allow_redirects=False,  # prevent HTTP→HTTPS redirect converting POST to GET
    )
    # If we get a redirect (3xx), follow it manually as POST to preserve the method
    if resp.is_redirect:
        location = resp.headers.get("Location", "")
        logger.info("exchange_code_for_token: following redirect to %s", location)
        resp = requests.post(
            location,
            data={
                "client_id": settings.INSTAGRAM_APP_ID,
                "client_secret": settings.INSTAGRAM_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=_TIMEOUT,
            allow_redirects=False,
        )
    data = resp.json() if resp.content else {}
    if resp.status_code != 200 or "access_token" not in data:
        raise InstagramOAuthError(
            (data.get("error_message") or "")
            or (data.get("error") or {}).get("message")
            or f"Token exchange failed ({resp.status_code})"
        )
    return data["access_token"]


def get_long_lived_token(short_lived_token: str) -> tuple[str, int | None]:
    """Exchange a short-lived token for a long-lived one (60 days).

    Returns ``(token, expires_in_seconds)``. On failure returns the original
    token with ``None`` so the connect flow is never blocked on this best-effort
    step (the short-lived token still works for about an hour).
    """
    if not settings.INSTAGRAM_APP_ID or not settings.INSTAGRAM_APP_SECRET:
        return short_lived_token, None
    resp = requests.get(
        f"{GRAPH_IG}/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.INSTAGRAM_APP_SECRET,
            "access_token": short_lived_token,
        },
        timeout=_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code == 200 and "access_token" in data:
        return data["access_token"], _as_int(data.get("expires_in"))
    logger.warning(
        "get_long_lived_token: failed (%s): %s", resp.status_code, resp.text[:300]
    )
    return short_lived_token, None


def refresh_long_lived_token(access_token: str) -> tuple[str, int | None]:
    """Refresh a long-lived token for another 60 days.

    Meta allows this once the token is at least 24 hours old and still valid.
    Raises InstagramOAuthError when Meta refuses (expired or revoked token).
    """
    resp = requests.get(
        f"{GRAPH_IG}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": access_token},
        timeout=_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code == 200 and "access_token" in data:
        return data["access_token"], _as_int(data.get("expires_in"))
    raise InstagramOAuthError(
        (data.get("error") or {}).get("message")
        or f"Token refresh failed ({resp.status_code})"
    )


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def discover_instagram_account(access_token: str) -> dict:
    """Return the Instagram Business Account linked to this token.

    With Instagram Login for Business the token is scoped directly to one
    Instagram account — no page walk is needed.  Returns a dict:
        {instagram_business_account_id, name, username, page_id, page_access_token}

    ``page_id`` is blank when not available; callers must handle that gracefully.

    Raises InstagramOAuthError if the /me call fails or returns no ID.
    """
    version = getattr(settings, "INSTAGRAM_GRAPH_VERSION", "v21.0")
    # Meta requires access_token as a query param (not Authorization header) and a versioned URL.
    # user_id is the actual IG account ID used in webhooks; id is the app-scoped ID.
    resp = requests.get(
        f"{GRAPH_IG}/{version}/me",
        params={
            "fields": "id,user_id,name,username",
            "access_token": access_token,
        },
        timeout=_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code != 200:
        raise InstagramOAuthError(
            (data.get("error") or {}).get("message")
            or f"Could not retrieve Instagram account ({resp.status_code})"
        )
    # user_id is the webhook-relevant IG Business Account ID; fall back to app-scoped id
    ig_id = data.get("user_id") or data.get("id")
    if not ig_id:
        raise InstagramOAuthError(
            f"Could not retrieve Instagram account ID ({resp.status_code}): {data}"
        )
    return {
        "instagram_business_account_id": ig_id,
        "name": data.get("name", ""),
        "username": data.get("username", ""),
        "page_id": "",
        "page_access_token": access_token,
    }


def subscribe_ig_account_to_webhooks(ig_user_id: str, access_token: str) -> bool:
    """Subscribe an Instagram Business Account to webhook fields.

    Uses the Instagram Graph API — works for Instagram Business Login accounts
    that have no linked Facebook Page.  Returns True on success.
    """
    version = getattr(settings, "INSTAGRAM_GRAPH_VERSION", "v21.0")
    resp = requests.post(
        f"{GRAPH_IG}/{version}/{ig_user_id}/subscribed_apps",
        params={
            "subscribed_fields": "messages,comments,mentions",
            "access_token": access_token,
        },
        timeout=_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    logger.info(
        "subscribe_ig_account_to_webhooks: ig_user_id=%s status=%s body=%s",
        ig_user_id,
        resp.status_code,
        resp.text[:500],
    )
    return resp.status_code == 200 and bool(data.get("success"))


def unsubscribe_ig_account_from_webhooks(ig_user_id: str, access_token: str) -> bool:
    """Stop Meta sending this account's events to Akilent (used on disconnect).

    Best-effort: returns False instead of raising, so a dead token never blocks
    a business from disconnecting.
    """
    version = getattr(settings, "INSTAGRAM_GRAPH_VERSION", "v21.0")
    try:
        resp = requests.delete(
            f"{GRAPH_IG}/{version}/{ig_user_id}/subscribed_apps",
            params={"access_token": access_token},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.warning("unsubscribe_ig_account_from_webhooks: %s", exc)
        return False
    if resp.status_code != 200:
        logger.warning(
            "unsubscribe_ig_account_from_webhooks: ig_user_id=%s status=%s body=%s",
            ig_user_id,
            resp.status_code,
            resp.text[:300],
        )
        return False
    return True


def parse_signed_request(signed_request: str) -> dict | None:
    """Verify and decode a Meta ``signed_request`` (deauthorize / data-deletion callbacks).

    Format: ``base64url(signature).base64url(json payload)``, where the signature is
    HMAC-SHA256 of the encoded payload keyed with the app secret. Returns the payload,
    or None if it is malformed or not signed by one of our secrets.
    """
    try:
        encoded_sig, payload = signed_request.split(".", 1)
        sig = _b64url_decode(encoded_sig)
        data = json.loads(_b64url_decode(payload))
    except (ValueError, AttributeError):
        return None
    for secret in (settings.INSTAGRAM_APP_SECRET, settings.WHATSAPP_APP_SECRET):
        if not secret:
            continue
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
        if hmac.compare_digest(sig, expected):
            return data
    return None


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def subscribe_page_to_webhooks(page_id: str, page_access_token: str) -> bool:
    """Subscribe to Instagram webhook fields via the connected Facebook Page.

    Only used for Facebook Login flow accounts that have a linked Facebook Page.
    For Instagram Business Login accounts use subscribe_ig_account_to_webhooks.
    Returns True on success; False otherwise (non-blocking).
    """
    if not page_id:
        logger.info(
            "subscribe_page_to_webhooks: no page_id — skipping programmatic subscription"
        )
        return False
    resp = requests.post(
        f"https://graph.facebook.com/{page_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {page_access_token}"},
        params={"subscribed_fields": "messages,comments,mentions,feed"},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning(
            "subscribe_page_to_webhooks: failed for page=%s (%s): %s",
            page_id,
            resp.status_code,
            resp.text[:300],
        )
        return False
    return True

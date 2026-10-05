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

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH_IG = "https://graph.instagram.com"
API_IG = "https://api.instagram.com"
_TIMEOUT = 15

SCOPES = (
    "instagram_business_basic,"
    "instagram_business_manage_messages,"
    "instagram_business_manage_comments,"
    "instagram_business_content_publish,"
    "instagram_business_manage_insights"
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


def get_long_lived_token(short_lived_token: str) -> str:
    """Exchange a short-lived token for a long-lived one (60 days).

    Returns the original token unchanged if the exchange fails, so the connect
    flow is never blocked on this best-effort step.
    """
    if not settings.INSTAGRAM_APP_ID or not settings.INSTAGRAM_APP_SECRET:
        return short_lived_token
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
        return data["access_token"]
    logger.warning(
        "get_long_lived_token: failed (%s): %s", resp.status_code, resp.text[:300]
    )
    return short_lived_token


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
    if resp.status_code == 200 and data.get("success"):
        return True
    return False


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

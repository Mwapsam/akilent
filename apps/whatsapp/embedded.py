"""WhatsApp Embedded Signup token exchange (Tech Provider onboarding).

After the customer completes Meta's Embedded Signup popup, the front end has:
  - an OAuth ``code`` (from FB.login),
  - the ``phone_number_id`` and ``waba_id`` (from the WA_EMBEDDED_SIGNUP message).

We exchange the code for a business-integration access token and (best-effort)
subscribe our app to the WABA so webhooks flow.
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
_TIMEOUT = 15


class EmbeddedSignupError(Exception):
    pass


def _version() -> str:
    return getattr(settings, "WHATSAPP_GRAPH_VERSION", "v21.0")


def exchange_code_for_token(code: str) -> str:
    """Exchange the Embedded Signup auth code for an access token."""
    if not settings.WHATSAPP_APP_ID or not settings.WHATSAPP_APP_SECRET:
        raise EmbeddedSignupError(
            "WHATSAPP_APP_ID and WHATSAPP_APP_SECRET must be configured."
        )
    resp = requests.get(
        f"{GRAPH}/{_version()}/oauth/access_token",
        params={
            "client_id": settings.WHATSAPP_APP_ID,
            "client_secret": settings.WHATSAPP_APP_SECRET,
            "code": code,
        },
        timeout=_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code != 200 or "access_token" not in data:
        raise EmbeddedSignupError(
            (data.get("error") or {}).get("message")
            or f"Token exchange failed ({resp.status_code})"
        )
    return data["access_token"]


def subscribe_app_to_waba(waba_id: str, access_token: str) -> None:
    """Subscribe our app to the WABA so message webhooks are delivered."""
    if not waba_id:
        return
    resp = requests.post(
        f"{GRAPH}/{_version()}/{waba_id}/subscribed_apps",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        logger.warning(
            "subscribe_app_to_waba: failed for waba=%s (%s): %s",
            waba_id, resp.status_code, resp.text[:300],
        )


def discover_waba_and_phone(access_token: str) -> tuple[list[str], dict]:
    """Find the WABA(s)/phone number(s) a redirect-flow token can access.

    The popup flow gets ``phone_number_id``/``waba_id`` for free via Meta's
    ``WA_EMBEDDED_SIGNUP`` postMessage. The redirect flow only gets a ``code``
    back, so we inspect the exchanged token's granular scopes to find which
    WABA(s) it was granted, then list phone numbers under each.

    Returns:
        (waba_ids, phone_numbers_by_waba) where ``phone_numbers_by_waba`` maps
        waba_id -> list of ``{"id": ..., "display_phone_number": ...}``.

    Raises:
        EmbeddedSignupError: if the token can't be inspected or has no WABA.
    """
    if not settings.WHATSAPP_APP_ID or not settings.WHATSAPP_APP_SECRET:
        raise EmbeddedSignupError(
            "WHATSAPP_APP_ID and WHATSAPP_APP_SECRET must be configured."
        )
    app_token = f"{settings.WHATSAPP_APP_ID}|{settings.WHATSAPP_APP_SECRET}"
    resp = requests.get(
        f"{GRAPH}/debug_token",
        params={"input_token": access_token, "access_token": app_token},
        timeout=_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    token_data = data.get("data") or {}
    if resp.status_code != 200 or not token_data.get("is_valid"):
        raise EmbeddedSignupError(
            (data.get("error") or {}).get("message") or "Could not verify access token."
        )
    logger.info("discover_waba_and_phone: debug_token data=%s", token_data)

    waba_ids: list[str] = []
    for scope in token_data.get("granular_scopes") or []:
        if scope.get("scope") == "whatsapp_business_management":
            waba_ids.extend(scope.get("target_ids") or [])

    if not waba_ids:
        # granular_scopes.target_ids often doesn't include a WABA that was
        # just *created* during Embedded Signup (as opposed to an existing
        # one the user already owned) — fall back to walking the businesses
        # this token can see and asking each for its owned WABAs.
        waba_ids = _discover_waba_ids_via_businesses(access_token)

    if not waba_ids:
        raise EmbeddedSignupError(
            "No WhatsApp Business Account was granted to this connection."
        )

    phone_numbers_by_waba: dict = {}
    for waba_id in waba_ids:
        resp = requests.get(
            f"{GRAPH}/{_version()}/{waba_id}/phone_numbers",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_TIMEOUT,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code == 200:
            phone_numbers_by_waba[waba_id] = data.get("data") or []
        else:
            logger.warning(
                "discover_waba_and_phone: phone_numbers list failed for waba=%s (%s): %s",
                waba_id, resp.status_code, resp.text[:300],
            )
            phone_numbers_by_waba[waba_id] = []

    logger.info(
        "discover_waba_and_phone: waba_ids=%s phone_numbers=%s",
        waba_ids, phone_numbers_by_waba,
    )
    return waba_ids, phone_numbers_by_waba


def _discover_waba_ids_via_businesses(access_token: str) -> list[str]:
    """Fallback: enumerate businesses this token can see and their owned WABAs.

    Used when ``debug_token``'s ``granular_scopes`` doesn't list a WABA
    directly — which happens for a WABA created fresh during Embedded Signup,
    since ``target_ids`` there tends to reflect pre-existing shared assets.
    """
    resp = requests.get(
        f"{GRAPH}/{_version()}/me/businesses",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=_TIMEOUT,
    )
    data = resp.json() if resp.content else {}
    if resp.status_code != 200:
        logger.warning(
            "discover_waba_and_phone: me/businesses failed (%s): %s",
            resp.status_code, resp.text[:300],
        )
        return []

    businesses = data.get("data") or []
    logger.info("discover_waba_and_phone: me/businesses=%s", businesses)

    waba_ids: list[str] = []
    for business in businesses:
        business_id = business.get("id")
        if not business_id:
            continue
        resp = requests.get(
            f"{GRAPH}/{_version()}/{business_id}/owned_whatsapp_business_accounts",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=_TIMEOUT,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            logger.warning(
                "discover_waba_and_phone: owned_whatsapp_business_accounts failed "
                "for business=%s (%s): %s",
                business_id, resp.status_code, resp.text[:300],
            )
            continue
        wabas = data.get("data") or []
        logger.info(
            "discover_waba_and_phone: business=%s owned_wabas=%s", business_id, wabas
        )
        waba_ids.extend(w["id"] for w in wabas if w.get("id"))

    return waba_ids


def register_phone_number(phone_number_id: str, access_token: str, pin: str) -> None:
    """Register a number on the Cloud API — required before it can send.

    Meta's Tech Provider Embedded Signup hands us a WABA + phone number, but the
    number is not usable on the Cloud API until it is registered (otherwise sends
    fail with error 133010). This call also sets the two-step verification PIN
    for numbers that don't have one yet.

    Raises:
        EmbeddedSignupError: if registration is rejected.
    """
    resp = requests.post(
        f"{GRAPH}/{_version()}/{phone_number_id}/register",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"messaging_product": "whatsapp", "pin": pin},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        data = resp.json() if resp.content else {}
        raise EmbeddedSignupError(
            (data.get("error") or {}).get("message")
            or f"Phone number registration failed ({resp.status_code})"
        )

"""Structured setup errors for the Instagram Business Login connect flow.

Mirrors apps/whatsapp/setup_errors.py. Callback branches pick a SetupError
code; the display strings live here. The error survives in the session until
a new connection attempt starts or one succeeds.
"""

from django.db import models
from django.shortcuts import redirect

SESSION_KEY = "instagram_setup_error"
CONNECT_URL = "/instagram/accounts/connect/oauth/"


class SetupError(models.TextChoices):
    NOT_CONFIGURED = "not_configured"
    STATE_EXPIRED = "state_expired"
    CANCELLED = "cancelled"
    NO_CODE = "no_code"
    TOKEN_EXCHANGE_FAILED = "token_exchange_failed"
    NO_ACCOUNTS = "no_accounts"
    SELECTION_EXPIRED = "selection_expired"
    CONNECT_REJECTED = "connect_rejected"


_TRY_AGAIN = {"action_label": "Connect with Instagram", "action_url": CONNECT_URL}

SETUP_ERRORS = {
    SetupError.NOT_CONFIGURED: {
        "title": "Instagram connection isn't available yet",
        "message": (
            "One-click setup isn't configured. "
            "Add an account manually below, or contact support."
        ),
        "action_label": "",
        "action_url": "",
    },
    SetupError.STATE_EXPIRED: {
        "title": "Your connection request expired",
        "message": (
            "For your security the sign-in link only works once and for a short time."
        ),
        **_TRY_AGAIN,
    },
    SetupError.CANCELLED: {
        "title": "Instagram setup wasn't completed",
        "message": "The Meta connection was cancelled before it finished.",
        **_TRY_AGAIN,
    },
    SetupError.NO_CODE: {
        "title": "Meta didn't finish the sign-in",
        "message": (
            "Meta didn't send back a sign-in code, so we couldn't connect your account."
        ),
        **_TRY_AGAIN,
    },
    SetupError.TOKEN_EXCHANGE_FAILED: {
        "title": "We couldn't finish connecting to Meta",
        "message": "Meta rejected the connection request.",
        **_TRY_AGAIN,
    },
    SetupError.NO_ACCOUNTS: {
        "title": "No Instagram Business Account found",
        "message": (
            "We couldn't find an Instagram Business Account connected to the "
            "Facebook Pages you manage. Connect a professional Instagram account "
            "to a Facebook Page in Meta Business Suite, then try again."
        ),
        **_TRY_AGAIN,
    },
    SetupError.SELECTION_EXPIRED: {
        "title": "Your account selection expired",
        "message": "Connect again and pick the account you want to use.",
        **_TRY_AGAIN,
    },
    SetupError.CONNECT_REJECTED: {
        "title": "This account couldn't be connected",
        "message": "",
        "action_label": "",
        "action_url": "",
    },
}


def redirect_with_setup_error(request, code, detail: str = ""):
    """Remember ``code`` in the session and send the user to the accounts page."""
    request.session[SESSION_KEY] = {"code": str(code), "detail": detail}
    return redirect("instagram-accounts")


def clear_setup_error(request):
    request.session.pop(SESSION_KEY, None)


def get_setup_error(request):
    """Return the presentation dict for the stored error, or None (non-consuming)."""
    stored = request.session.get(SESSION_KEY)
    if not stored:
        return None
    try:
        info = SETUP_ERRORS[SetupError(stored.get("code"))]
    except ValueError:
        return None
    return {**info, "code": stored["code"], "detail": stored.get("detail", "")}

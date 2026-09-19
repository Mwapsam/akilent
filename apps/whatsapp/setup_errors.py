"""Structured setup errors for the Meta connect flow.

Callback branches pick a ``SetupError`` code; the words live in one place
(``SETUP_ERRORS``). The error is kept in the session until the user starts a new
connection or one succeeds, so it survives reloads and always has a next action.
"""
from django.db import models
from django.shortcuts import redirect

SESSION_KEY = "whatsapp_setup_error"
CONNECT_URL = "/whatsapp/connect/redirect/"
META_HELP_URL = "#wa-meta-requirements"


class SetupError(models.TextChoices):
    NOT_CONFIGURED = "not_configured"
    STATE_EXPIRED = "state_expired"
    CANCELLED = "cancelled"
    NO_CODE = "no_code"
    TOKEN_EXCHANGE_FAILED = "token_exchange_failed"
    NO_WABA = "no_waba"
    NO_PHONE = "no_phone"
    SELECTION_EXPIRED = "selection_expired"
    CONNECT_REJECTED = "connect_rejected"


_TRY_AGAIN = {"action_label": "Connect with WhatsApp", "action_url": CONNECT_URL}

SETUP_ERRORS = {
    SetupError.NOT_CONFIGURED: {
        "title": "WhatsApp connection isn't available yet",
        "message": "One-click setup isn't configured. Add a number manually below, or contact support.",
        "action_label": "", "action_url": "",
    },
    SetupError.STATE_EXPIRED: {
        "title": "Your connection request expired",
        "message": "For your security the sign-in link only works once and for a short time.",
        **_TRY_AGAIN,
    },
    SetupError.CANCELLED: {
        "title": "WhatsApp setup wasn't completed",
        "message": "The Meta connection was cancelled before it finished.",
        **_TRY_AGAIN,
    },
    SetupError.NO_CODE: {
        "title": "Meta didn't finish the sign-in",
        "message": "Meta didn't send back a sign-in code, so we couldn't connect your account.",
        **_TRY_AGAIN,
    },
    SetupError.TOKEN_EXCHANGE_FAILED: {
        "title": "We couldn't finish connecting to Meta",
        "message": "Meta rejected the connection request.",
        **_TRY_AGAIN,
    },
    SetupError.NO_WABA: {
        "title": "WhatsApp Business Account not found",
        "message": (
            "We couldn't find a WhatsApp Business Account in the Meta account you "
            "selected. Select or create one during sign-in."
        ),
        **_TRY_AGAIN, "help_label": "What you need from Meta", "help_url": META_HELP_URL,
    },
    SetupError.NO_PHONE: {
        "title": "Your WhatsApp Business Account has no phone number",
        "message": "Add a phone number in Meta Business Manager, then connect again.",
        **_TRY_AGAIN, "help_label": "What you need from Meta", "help_url": META_HELP_URL,
    },
    SetupError.SELECTION_EXPIRED: {
        "title": "Your number selection expired",
        "message": "Connect again and pick the number you want to use.",
        **_TRY_AGAIN,
    },
    SetupError.CONNECT_REJECTED: {
        "title": "This number couldn't be connected",
        "message": "",
        "action_label": "", "action_url": "",
    },
}


def redirect_with_setup_error(request, code, detail: str = ""):
    """Remember ``code`` for the numbers page and send the user there."""
    request.session[SESSION_KEY] = {"code": str(code), "detail": detail}
    return redirect("whatsapp-numbers")


def clear_setup_error(request):
    request.session.pop(SESSION_KEY, None)


def get_setup_error(request):
    """Presentation dict for the stored error, or None. Does not consume it."""
    stored = request.session.get(SESSION_KEY)
    if not stored:
        return None
    try:
        info = SETUP_ERRORS[SetupError(stored.get("code"))]
    except ValueError:
        return None
    return {**info, "code": stored["code"], "detail": stored.get("detail", "")}

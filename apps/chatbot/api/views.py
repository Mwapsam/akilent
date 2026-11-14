"""
Public chatbot API — JSON endpoints consumed by the website widget.

All endpoints are unauthenticated (no Django session/cookie required) and
secured via public_key + Origin + rate limiting instead. They must never
expose internal IDs or tenant data beyond PublicChatbotConfig fields.

The widget runs on the business's own website, so every endpoint answers the
CORS preflight and only lets an allowed origin read its response (see
``auth.with_cors``). The widget derives the API origin from its own script URL
and then uses the ``api_origin`` the init response returns.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.chatbot.api import auth, serializers

if TYPE_CHECKING:
    from apps.chatbot.models import ChatbotConfig, ChatSession

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 8_192  # 8 KB — rejects oversized payloads before parsing
_MAX_MESSAGE_CHARS = 2_000  # single visitor message length cap
SESSION_TTL_HOURS = 24  # sessions idle longer than this are rejected
_MAX_CHOICE_CHARS = 300  # a tapped option's value: "<interaction token>:<option id>"


def _parse_body(request: HttpRequest) -> dict | None:
    """Return parsed JSON or None if the body is malformed or oversized."""
    if len(request.body) > _MAX_BODY_BYTES:
        return None
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            return None
        return data
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


_SAFE_ERRORS: frozenset[str] = frozenset(
    [
        "Invalid request.",
        "session_key and message are required.",
        "Message too long.",
        "Session not found.",
        "Session expired.",
        "Origin not authorized.",
        "Rate limit exceeded. Please slow down.",
        "Invalid or inactive chatbot key.",
        "session_key is required.",
    ]
)


def _sanitise_error(msg: str) -> str:
    """Return a safe, generic error string — never internal state."""
    return msg if msg in _SAFE_ERRORS else "An error occurred."


def _session_for(request: HttpRequest, session_key: str, *, polling: bool = False):
    """(session, None) for a usable session, else (None, error response).

    Order matters: the Origin is checked (and CORS allowed) before the rate limit and the
    expiry, so the widget on an allowed site can read a 429 or 410 and say what happened, while
    an unlisted site learns nothing. A made-up key stops at the indexed lookup and never creates
    a rate-limit entry.
    """
    from apps.chatbot.models import ChatSession

    session = (
        ChatSession.objects.select_related("chatbot", "chatbot__account")
        .filter(session_key=session_key)
        .first()
    )
    if session is None:
        return None, JsonResponse({"error": "Session not found."}, status=404)
    if not session.chatbot.is_active:
        return None, auth.forbidden("Invalid or inactive chatbot key.")
    if not auth.check_origin(session.chatbot, request):
        return None, auth.forbidden("Origin not authorized.")
    auth.allow_cors(request)
    within = (
        auth.check_poll_rate_limit(session_key)
        if polling
        else auth.check_rate_limit(session_key)
    )
    if not within:
        return None, auth.rate_limited()
    if _is_session_expired(session):
        return None, JsonResponse({"error": "Session expired."}, status=410)
    return session, None


@csrf_exempt
@auth.with_cors
@require_POST
def init(request: HttpRequest) -> JsonResponse:
    """POST {public_key, session_key?} → {session_key, api_origin, config, messages}

    A ``session_key`` the widget kept from earlier (same chatbot, not expired) is resumed, with
    the chat so far in ``messages``, so a page reload or the next page keeps the conversation.
    Anything else starts a new session.
    """
    data = _parse_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    public_key = data.get("public_key", "")

    if not auth.check_init_rate_limit(auth.client_ip(request)):
        return auth.rate_limited()

    chatbot_obj = auth.resolve_chatbot(public_key)
    if chatbot_obj is None:
        return auth.forbidden("Invalid or inactive chatbot key.")

    from apps.chatbot.services import install

    origin = request.META.get("HTTP_ORIGIN", "")
    if not auth.check_origin(chatbot_obj, request):
        install.record_blocked(chatbot_obj, origin)  # shown to the owner on Install
        return auth.forbidden("Origin not authorized.")
    auth.allow_cors(request)
    install.record_seen(chatbot_obj, origin)

    api_origin = getattr(settings, "SITE_URL", "").rstrip("/")
    if not api_origin:
        # Fail before creating the session — an orphaned session_key with an
        # empty api_origin is useless to the widget and wastes a DB row.
        logger.error(
            "SITE_URL is not configured — chatbot init refused to avoid orphaned sessions"
        )
        return JsonResponse({"error": "Service misconfiguration."}, status=503)

    from apps.chatbot.models import ChatSession
    from apps.chatbot.services.conversation import current_prompt, messages_after

    chatbot: ChatbotConfig = chatbot_obj  # type: ignore[assignment]
    session = _resumable_session(chatbot, data.get("session_key"))
    if session is None:
        session = ChatSession.objects.create(chatbot=chatbot)
        messages: list[dict] = []
        cursor = 0
    else:
        messages, cursor = messages_after(session, None)

    return JsonResponse(
        {
            "session_key": session.session_key,
            "api_origin": api_origin,
            "config": serializers.public_config(chatbot),
            "messages": messages,
            "cursor": cursor,
            "expect": current_prompt(session) if messages else None,
        }
    )


def _resumable_session(chatbot: ChatbotConfig, session_key) -> ChatSession | None:
    from apps.chatbot.models import ChatSession

    if not isinstance(session_key, str) or not session_key:
        return None
    session = ChatSession.objects.filter(
        session_key=session_key, chatbot=chatbot
    ).first()
    if session is None or _is_session_expired(session):
        return None
    return session


@csrf_exempt
@auth.with_cors
@require_POST
def message(request: HttpRequest) -> JsonResponse:
    """POST {session_key, message, choice?} → {reply, messages, handoff_suggested, cursor, expect}

    ``choice`` is the value of a tapped option (see ``current_prompt``), sent with its title as
    ``message``.
    """
    data = _parse_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)

    session_key = data.get("session_key", "")
    body = str(data.get("message") or "").strip()

    if not isinstance(session_key, str) or not session_key or not body:
        return JsonResponse(
            {"error": "session_key and message are required."}, status=400
        )

    if len(body) > _MAX_MESSAGE_CHARS:
        return JsonResponse({"error": "Message too long."}, status=400)

    session, error = _session_for(request, session_key)
    if error is not None:
        return error

    choice = data.get("choice") or ""
    if not isinstance(choice, str) or len(choice) > _MAX_CHOICE_CHARS:
        return JsonResponse({"error": "Invalid request."}, status=400)

    from apps.chatbot.services.inbound import handle_visitor_message

    return JsonResponse(handle_visitor_message(session, body, choice=choice))


@csrf_exempt
@auth.with_cors
@require_POST
def open_chat(request: HttpRequest) -> JsonResponse:
    """POST {session_key} → {messages, cursor}: the visitor opened the chat window.

    Runs the bot's "visitor opens the chat" automation the first time in a session.
    """
    data = _parse_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    session_key = data.get("session_key", "")
    if not isinstance(session_key, str) or not session_key:
        return JsonResponse({"error": "session_key is required."}, status=400)
    session, error = _session_for(request, session_key)
    if error is not None:
        return error

    from apps.chatbot.services.inbound import handle_opened

    return JsonResponse(handle_opened(session))


@csrf_exempt
@auth.with_cors
@require_POST
def poll(request: HttpRequest) -> JsonResponse:
    """POST {session_key, cursor} → {messages, cursor}: anything said since ``cursor``.

    How replies from the team (sent from the inbox) and from automations that answer later
    reach the visitor. POST, so the session key never sits in a URL or an access log.
    """
    data = _parse_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    session_key = data.get("session_key", "")
    if not isinstance(session_key, str) or not session_key:
        return JsonResponse({"error": "session_key is required."}, status=400)
    cursor = data.get("cursor", 0)
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        return JsonResponse({"error": "Invalid request."}, status=400)
    session, error = _session_for(request, session_key, polling=True)
    if error is not None:
        return error

    from apps.chatbot.services.conversation import current_prompt, messages_after

    if not session.conversation_id:
        return JsonResponse({"messages": [], "cursor": 0, "expect": None})
    messages, new_cursor = messages_after(session, cursor) if cursor else ([], None)
    if new_cursor is None:
        # A widget with no cursor yet starts from now rather than replaying the chat.
        messages, new_cursor = messages_after(session, None)
    return JsonResponse(
        {"messages": messages, "cursor": new_cursor, "expect": current_prompt(session)}
    )


@csrf_exempt
@auth.with_cors
@require_POST
def identify(request: HttpRequest) -> JsonResponse:
    """POST {session_key, email?, phone?, name?} → {identified: bool}"""
    data = _parse_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    session_key = data.get("session_key", "")

    if not isinstance(session_key, str) or not session_key:
        return JsonResponse({"error": "session_key is required."}, status=400)

    session, error = _session_for(request, session_key)
    if error is not None:
        return error

    email = str(data.get("email") or "").strip()
    name = str(data.get("name") or "").strip()
    phone = str(data.get("phone") or "").strip()

    # Validate email format before persisting — raw save() bypasses EmailField
    # validation, so we call the validator explicitly.
    if email:
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.core.validators import validate_email

        try:
            validate_email(email)
        except DjangoValidationError:
            return JsonResponse({"error": "Invalid email address."}, status=400)

    from apps.chatbot.services.conversation import identify_visitor

    identified = identify_visitor(session, name=name, email=email, phone=phone)
    return JsonResponse({"identified": identified})


def _is_session_expired(session: ChatSession) -> bool:
    from datetime import timedelta

    # Use last_activity_at so active sessions don't hard-expire mid-conversation.
    # started_at is when the session was created; last_activity_at is updated on
    # every message and identify call, giving visitors the full TTL from their
    # most recent interaction.
    cutoff = timezone.now() - timedelta(hours=SESSION_TTL_HOURS)
    return session.last_activity_at < cutoff  # type: ignore[operator]

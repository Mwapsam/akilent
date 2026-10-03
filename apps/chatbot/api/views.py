"""
Public chatbot API — three JSON endpoints consumed by the CDN widget.

All endpoints are unauthenticated (no Django session/cookie required) and
secured via public_key + Origin + rate limiting instead. They must never
expose internal IDs or tenant data beyond PublicChatbotConfig fields.

The widget uses absolute URLs: it reads api_origin from the init response
and calls all subsequent endpoints relative to that origin.
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
    from apps.chatbot.models import ChatSession

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 8_192  # 8 KB — rejects oversized payloads before parsing
_MAX_MESSAGE_CHARS = 2_000  # single visitor message length cap
SESSION_TTL_HOURS = 24  # sessions older than this are rejected


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


@csrf_exempt
@require_POST
def init(request: HttpRequest) -> JsonResponse:
    """POST {public_key} → {session_key, api_origin, config}"""
    data = _parse_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    public_key = data.get("public_key", "")

    ip = _client_ip(request)
    if not auth.check_init_rate_limit(ip):
        return auth.rate_limited()

    chatbot_obj = auth.resolve_chatbot(public_key)
    if chatbot_obj is None:
        return auth.forbidden("Invalid or inactive chatbot key.")

    if not auth.check_origin(chatbot_obj, request):
        return auth.forbidden("Origin not authorized.")

    from apps.chatbot.models import ChatbotConfig as _ChatbotConfig
    from apps.chatbot.models import ChatSession

    api_origin = getattr(settings, "SITE_URL", "").rstrip("/")
    if not api_origin:
        # Fail before creating the session — an orphaned session_key with an
        # empty api_origin is useless to the widget and wastes a DB row.
        logger.error(
            "SITE_URL is not configured — chatbot init refused to avoid orphaned sessions"
        )
        return JsonResponse({"error": "Service misconfiguration."}, status=503)

    chatbot: _ChatbotConfig = chatbot_obj  # type: ignore[assignment]
    session = ChatSession.objects.create(chatbot=chatbot)

    return JsonResponse(
        {
            "session_key": session.session_key,
            "api_origin": api_origin,
            "config": serializers.public_config(chatbot),
        }
    )


@csrf_exempt
@require_POST
def message(request: HttpRequest) -> JsonResponse:
    """POST {session_key, message} → {reply, handoff_suggested, ticket_number}"""
    data = _parse_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)

    session_key = data.get("session_key", "")
    body = (data.get("message") or "").strip()

    if not session_key or not body:
        return JsonResponse(
            {"error": "session_key and message are required."}, status=400
        )

    if len(body) > _MAX_MESSAGE_CHARS:
        return JsonResponse({"error": "Message too long."}, status=400)

    if not auth.check_rate_limit(session_key):
        return auth.rate_limited()

    from apps.chatbot.models import ChatSession

    try:
        session = ChatSession.objects.select_related("chatbot", "chatbot__account").get(
            session_key=session_key
        )
    except ChatSession.DoesNotExist:
        return JsonResponse({"error": "Session not found."}, status=404)

    if not session.chatbot.is_active:
        return auth.forbidden("Invalid or inactive chatbot key.")

    if _is_session_expired(session):
        return JsonResponse({"error": "Session expired."}, status=410)

    if not auth.check_origin(session.chatbot, request):
        return auth.forbidden("Origin not authorized.")

    from apps.chatbot.services.responder import handle_message

    result = handle_message(session, body)
    return JsonResponse(result)


@csrf_exempt
@require_POST
def identify(request: HttpRequest) -> JsonResponse:
    """POST {session_key, email?, phone?, name?} → {identified: bool}"""
    data = _parse_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    session_key = data.get("session_key", "")

    if not session_key:
        return JsonResponse({"error": "session_key is required."}, status=400)

    if not auth.check_rate_limit(session_key):
        return auth.rate_limited()

    from apps.chatbot.models import ChatSession

    try:
        session = ChatSession.objects.select_related("chatbot").get(
            session_key=session_key
        )
    except ChatSession.DoesNotExist:
        return JsonResponse({"error": "Session not found."}, status=404)

    if not session.chatbot.is_active:
        return auth.forbidden("Invalid or inactive chatbot key.")

    if _is_session_expired(session):
        return JsonResponse({"error": "Session expired."}, status=410)

    if not auth.check_origin(session.chatbot, request):
        return auth.forbidden("Origin not authorized.")

    email = (data.get("email") or "").strip()
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()

    # Validate email format before persisting — raw save() bypasses EmailField
    # validation, so we call the validator explicitly.
    if email:
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.core.validators import validate_email

        try:
            validate_email(email)
        except DjangoValidationError:
            return JsonResponse({"error": "Invalid email address."}, status=400)

    # Store visitor-supplied fields for display context only — never auto-link to
    # an existing Contact record based on self-reported email/phone, as there is
    # no proof of identity. Contact attribution requires a verifiable server-side
    # signal (e.g., a registered action that checks an order token).
    update_fields: list[str] = ["last_activity_at"]
    if name:
        session.visitor_name = name[:200]
        update_fields.append("visitor_name")
    if email:
        session.visitor_email = email[:254]
        update_fields.append("visitor_email")
    if phone:
        session.visitor_phone = phone[:30]
        update_fields.append("visitor_phone")

    identified = bool(name or email)
    if identified and session.identified_at is None:
        from django.utils import timezone as _tz

        session.identified_at = _tz.now()
        update_fields.append("identified_at")

    session.save(update_fields=update_fields)
    return JsonResponse({"identified": identified})


def _client_ip(request: HttpRequest) -> str:
    """Return the real client IP.

    Only trusts X-Forwarded-For when CHATBOT_TRUST_PROXY_HEADERS=True is set
    in settings (i.e. the deployment sits behind a known reverse proxy). Without
    that flag, REMOTE_ADDR is used so the header cannot be spoofed to bypass
    the init rate limit.
    """
    # Default True: this service always runs behind nginx which sets
    # X-Forwarded-For from $remote_addr (not from the client), so the header
    # is trustworthy. Set CHATBOT_TRUST_PROXY_HEADERS=False only when running
    # without a reverse proxy (e.g. direct internet exposure in dev/staging).
    trust_proxy = getattr(settings, "CHATBOT_TRUST_PROXY_HEADERS", True)
    if trust_proxy:
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _is_session_expired(session: ChatSession) -> bool:
    from datetime import timedelta

    # Use last_activity_at so active sessions don't hard-expire mid-conversation.
    # started_at is when the session was created; last_activity_at is updated on
    # every message and identify call, giving visitors the full TTL from their
    # most recent interaction.
    cutoff = timezone.now() - timedelta(hours=SESSION_TTL_HOURS)
    return session.last_activity_at < cutoff  # type: ignore[operator]

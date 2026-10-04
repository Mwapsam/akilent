"""
Security checks applied to all public chatbot API endpoints.

Layered defence:
  1. public_key exists and is_active=True
  2. Origin header is in chatbot_config.allowed_domains (empty list = deny all)
  3. Rate limit per session_key via Django cache (20 msg/min)

None of these checks touch apps.support or any tenant-internal model.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.http import HttpRequest, JsonResponse

if TYPE_CHECKING:
    from apps.chatbot.models import ChatbotConfig

logger = logging.getLogger(__name__)

_RATE_LIMIT = 20  # messages per window
_RATE_WINDOW = 60  # seconds


def resolve_chatbot(public_key: str) -> object | None:
    """Return the ChatbotConfig for this public_key if active, else None.

    Also verifies that all linked knowledge sources belong to the same account
    as the chatbot, logging a warning if any cross-account link is found (this
    should never happen given the service-layer guards, but provides runtime
    observability).
    """
    from apps.chatbot.models import ChatbotConfig, ChatbotKnowledgeSource

    chatbot = ChatbotConfig.objects.filter(
        public_key=public_key, is_active=True
    ).first()
    if chatbot is None:
        return None

    # Integrity check: log cross-account knowledge links but do NOT mutate here.
    # Call purge_cross_account_knowledge_links(chatbot) separately when a write
    # is appropriate (e.g. a background task or an explicit admin action).
    bad_count = (
        ChatbotKnowledgeSource.objects.filter(chatbot=chatbot, is_active=True)
        .exclude(knowledge_entry__account_id=chatbot.account_id)
        .count()
    )
    if bad_count:
        logger.warning(
            "Chatbot %s (pk=%s) has %d cross-account knowledge link(s) — "
            "call purge_cross_account_knowledge_links() to disable them.",
            chatbot.public_key,
            chatbot.pk,
            bad_count,
        )

    return chatbot


def purge_cross_account_knowledge_links(chatbot: ChatbotConfig) -> int:
    """Disable any knowledge sources that belong to a different account than the chatbot.

    Returns the number of links disabled. This is a deliberate write operation
    and must not be called from read-only resolution paths.
    """
    from apps.chatbot.models import ChatbotKnowledgeSource

    bad_links = ChatbotKnowledgeSource.objects.filter(
        chatbot=chatbot, is_active=True
    ).exclude(knowledge_entry__account_id=chatbot.account_id)
    count = bad_links.count()
    if count:
        bad_links.update(is_active=False)
        logger.warning(
            "Purged %d cross-account knowledge link(s) from chatbot %s (pk=%s).",
            count,
            chatbot.public_key,
            chatbot.pk,
        )
    return count


def check_origin(chatbot: object, request: HttpRequest) -> bool:
    """Return True if the request Origin is in chatbot.allowed_domains.

    An empty allowed_domains list means *no* origin is authorized.

    When CHATBOT_REQUIRE_ORIGIN=False (default: True), requests without an
    Origin header are allowed through. Set to False only for server-side
    integrations (e.g. curl, Postman, backend-to-backend) where browsers are
    not involved and CORS protection is irrelevant.
    """
    from django.conf import settings

    origin = request.META.get("HTTP_ORIGIN", "")

    # Always allow the platform's own origin so the dashboard preview works
    # without requiring operators to add their own domain to allowed_domains.
    # This check runs before the allowed_domains guard so it works even when
    # the operator hasn't configured any allowed domains yet.
    site_url = getattr(settings, "SITE_URL", "").rstrip("/")
    if site_url and origin.rstrip("/") == site_url:
        return True

    allowed: list[str] = chatbot.allowed_domains  # type: ignore[attr-defined]
    if not allowed:
        return False

    if not origin:
        # Browsers always send Origin on cross-origin requests, so a missing
        # header means a non-browser caller. Let the operator opt in to
        # allowing these via CHATBOT_REQUIRE_ORIGIN=False.
        require_origin: bool = getattr(settings, "CHATBOT_REQUIRE_ORIGIN", True)
        return not require_origin

    return any(origin.rstrip("/") == d.rstrip("/") for d in allowed)


_INIT_RATE_LIMIT = 10  # init calls per window per IP
_INIT_RATE_WINDOW = 60  # seconds


def check_rate_limit(session_key: str) -> bool:
    """Return True if this session_key is within the rate limit.

    Uses add+incr for atomicity: cache.add only succeeds once (sets the key to 1
    with TTL), and cache.incr is atomic on all supported backends.
    """
    cache_key = f"chatbot_rate:{session_key}"
    if cache.add(cache_key, 1, timeout=_RATE_WINDOW):
        # Key did not exist — this is the first message in the window.
        return True
    try:
        count = cache.incr(cache_key)
    except ValueError:
        # Key expired between add and incr — treat as the first message.
        return True
    return count <= _RATE_LIMIT


def check_init_rate_limit(ip: str) -> bool:
    """Return True if this IP is within the init endpoint rate limit."""
    cache_key = f"chatbot_init_rate:{ip}"
    if cache.add(cache_key, 1, timeout=_INIT_RATE_WINDOW):
        return True
    try:
        count = cache.incr(cache_key)
    except ValueError:
        return True
    return count <= _INIT_RATE_LIMIT


def forbidden(reason: str) -> JsonResponse:
    return JsonResponse({"error": reason}, status=403)


def rate_limited() -> JsonResponse:
    return JsonResponse({"error": "Rate limit exceeded. Please slow down."}, status=429)

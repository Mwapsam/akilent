"""A website chatbot's daily AI allowance (``ChatbotConfig.ai_daily_limit``).

Every AI use in a bot's chats takes one turn: the bot's own answers (``responder``) and the AI
steps of its workflow (``apps.automation.ai_steps``, through ``take_ai_turn`` in
``apps.chatbot.api``). The business's plan limit (``ai_actions_day``) still applies on top; this
only stops one public widget from using all of it. Counted per UTC day in the cache.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

_TTL_SECONDS = 26 * 3600


def _key(chatbot_id: int) -> str:
    return f"chatbot_ai_day:{chatbot_id}:{timezone.now().date().isoformat()}"


def used_today(chatbot_id: int) -> int:
    return int(cache.get(_key(chatbot_id)) or 0)


def take_ai_turn(chatbot_id: int | None) -> bool:
    """Use one of the bot's AI turns for today. False when none are left (or the bot is gone)."""
    from apps.chatbot.models import ChatbotConfig

    if not chatbot_id:
        return True  # not a chatbot run: only the business's own limits apply
    limit = (
        ChatbotConfig.objects.filter(pk=chatbot_id)
        .values_list("ai_daily_limit", flat=True)
        .first()
    )
    if limit is None or limit <= 0:
        return False
    key = _key(chatbot_id)
    cache.add(key, 0, _TTL_SECONDS)
    try:
        used = cache.incr(key)
    except ValueError:
        cache.set(key, 1, _TTL_SECONDS)
        used = 1
    if used > limit:
        if used == limit + 1:
            logger.info("chatbot %s reached its daily AI limit (%s)", chatbot_id, limit)
        return False
    return True

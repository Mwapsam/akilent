"""The Install check: has the widget loaded on the owner's website yet?

``init`` records the origin each time the widget starts. An allowed origin is saved on the bot
(``ChatbotConfig.seen_on``) at most once an hour. An origin that was refused is kept briefly in
the cache, a handful per bot, so the owner can see "it tried to load on www.example.com, which
isn't in your list", the most common install mistake. Nothing about visitors is stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.chatbot.domains import normalise_origin

_RESAVE_AFTER = timedelta(hours=1)
_BLOCKED_KEEP_SECONDS = 7 * 24 * 3600
_BLOCKED_MAX = 5


def _own_origin() -> str | None:
    return normalise_origin(getattr(settings, "SITE_URL", ""))


def _origin(raw: str) -> str | None:
    origin = normalise_origin(raw[:300]) if raw else None
    if origin is None or len(origin) > 260 or origin == _own_origin():
        return None  # Akilent's own pages (the dashboard) aren't the owner's website
    return origin


def record_seen(bot, raw_origin: str) -> None:
    """The widget loaded on an allowed site."""
    from apps.chatbot.models import ChatbotConfig

    origin = _origin(raw_origin)
    if origin is None:
        return
    now = timezone.now()
    last = _parse((bot.seen_on or {}).get(origin))
    if last is not None and now - last < _RESAVE_AFTER:
        return
    seen = {**(bot.seen_on or {}), origin: now.isoformat()}
    bot.seen_on = seen
    # A plain update: no validation pass or updated_at change for a background stamp.
    ChatbotConfig.objects.filter(pk=bot.pk).update(seen_on=seen)


def record_blocked(bot, raw_origin: str) -> None:
    """The widget tried to load on a site that isn't in the bot's list."""
    origin = _origin(raw_origin)
    if origin is None:
        return
    key = _blocked_key(bot.pk)
    blocked = cache.get(key) or {}
    if origin in blocked:
        return
    if len(blocked) >= _BLOCKED_MAX:
        return  # enough to show the owner; a flood of made-up origins changes nothing
    blocked[origin] = timezone.now().isoformat()
    cache.set(key, blocked, _BLOCKED_KEEP_SECONDS)


def status(bot) -> dict:
    """``{"sites": [{"origin", "seen_at"}], "blocked": [{"origin", "seen_at"}], "live": bool}``.

    ``sites`` lists each allowed domain with when the widget last loaded there (None = not yet).
    ``blocked`` lists refused origins that are still not allowed.
    """
    seen = bot.seen_on or {}
    allowed = list(bot.allowed_domains or [])
    sites = [{"origin": d, "seen_at": _parse(seen.get(d))} for d in allowed]
    blocked = [
        {"origin": origin, "seen_at": _parse(when)}
        for origin, when in (cache.get(_blocked_key(bot.pk)) or {}).items()
        if origin not in allowed
    ]
    return {
        "sites": sites,
        "blocked": blocked,
        "live": any(s["seen_at"] for s in sites),
    }


def _blocked_key(bot_id: int) -> str:
    return f"chatbot_blocked_origins:{bot_id}"


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

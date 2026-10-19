"""Public, signed-out endpoints for Content Studio: the tracked WhatsApp link."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.core.cache import cache
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .links import is_bot, visitor_hash, whatsapp_url
from .models import LinkClick, TrackedLink

logger = logging.getLogger(__name__)

_CLICK_LIMIT = 30  # recorded clicks per visitor per window; the redirect always works
_CLICK_WINDOW = 60  # seconds


def _within_click_limit(key: str) -> bool:
    cache_key = f"golink:{key}"
    if cache.add(cache_key, 1, timeout=_CLICK_WINDOW):
        return True
    try:
        return cache.incr(cache_key) <= _CLICK_LIMIT
    except ValueError:
        return True


@require_GET
@never_cache
def go_link(request, code: str):
    """Count the click, then open WhatsApp with the post's prefilled message."""
    link = TrackedLink.objects.filter(
        code=code.upper(), disabled_at__isnull=True
    ).first()
    if link is None:
        return render(request, "publishing/link_not_found.html", status=404)

    ua = request.META.get("HTTP_USER_AGENT", "")[:500]
    ip = request.META.get("REMOTE_ADDR", "")
    visitor = visitor_hash(ip, ua)
    if _within_click_limit(visitor):
        try:
            LinkClick.objects.create(
                link=link,
                visitor_hash=visitor,
                referer_host=(
                    urlparse(request.META.get("HTTP_REFERER", "")).hostname or ""
                )[:255],
                is_bot=is_bot(ua),
            )
        except Exception:
            # Counting must never stop the customer from reaching the chat.
            logger.exception("Couldn't record a click for link=%s", link.pk)
    return HttpResponseRedirect(whatsapp_url(link))

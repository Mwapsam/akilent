"""The subscribe side of real-time updates (see apps/core/realtime.py for publish).

An SSE stream, not a websocket: every update flows server -> browser only (sending a message
stays a normal POST through the existing views), and SSE gets the browser's automatic
reconnect-with-backoff and plain-HTTP simplicity for free.

Meant to run behind a *separate* ASGI process (see docker-compose.yml's ``events`` service and
docker/entrypoint_events.sh) — the main web tier stays on the proven WSGI/gunicorn path from the
Operational Readiness Gate. This module only needs Django's ORM and session/auth, which both
processes share via the same settings and database.
"""

from __future__ import annotations

import asyncio
import json
import logging

from django.conf import settings
from django.http import HttpResponseForbidden, StreamingHttpResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 20
# A runaway or forgotten tab shouldn't be able to open unlimited long-lived connections to a
# process with a fixed worker pool; this is a soft cap per account; it's a dict on the (single)
# ASGI worker process, not shared across workers — good enough for the pilot's connection counts.
MAX_CONNECTIONS_PER_ACCOUNT = 20
_connections_by_account: dict[int, int] = {}


async def _resolve_account_id(request):
    """Sync auth/session/membership work, done once, off the event loop."""
    from asgiref.sync import sync_to_async

    def _resolve():
        if not request.user.is_authenticated:
            return None
        from apps.accounts.utils import get_current_account

        account = get_current_account(request)
        return account.id if account is not None else None

    return await sync_to_async(_resolve, thread_sensitive=True)()


async def event_stream(request):
    """``GET /events/stream/`` — one Server-Sent-Events connection per browser tab.

    static/js/vendor/htmx-ext-sse.js opens this from the app shell (templates/base.html,
    authenticated pages only) and keeps it open across in-place navigations; descendants declare
    what they care about with ``hx-trigger="sse:<event>"`` or ``sse-swap``.
    """
    account_id = await _resolve_account_id(request)
    if account_id is None:
        return HttpResponseForbidden("Sign in required.")

    if _connections_by_account.get(account_id, 0) >= MAX_CONNECTIONS_PER_ACCOUNT:
        return HttpResponseForbidden("Too many open connections.")

    response = StreamingHttpResponse(
        _stream(account_id),
        content_type="text/event-stream",
    )
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # nginx: don't buffer an SSE response
    return response


async def _stream(account_id: int):
    from apps.core.realtime import channel_name

    _connections_by_account[account_id] = _connections_by_account.get(account_id, 0) + 1
    yield "retry: 3000\n\n"

    redis_client = await _redis_or_none()
    if redis_client is None:
        # No Redis: say so once and hold the connection open on heartbeats alone. The client's
        # own poll-fallback (it never got an sse:* event) keeps everything working either way.
        try:
            while True:
                yield ": no realtime backend, polling continues\n\n"
                await asyncio.sleep(HEARTBEAT_SECONDS)
        finally:
            _connections_by_account[account_id] = max(
                0, _connections_by_account.get(account_id, 1) - 1
            )
        return

    pubsub = redis_client.pubsub()
    try:
        await pubsub.subscribe(channel_name(account_id))
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=HEARTBEAT_SECONDS
            )
            if message is None:
                yield f": keep-alive {timezone.now().isoformat()}\n\n"
                continue
            try:
                data = json.loads(message["data"])
            except (TypeError, ValueError):
                continue
            event = data.get("event", "message")
            yield "event: {}\ndata: {}\n\n".format(event, json.dumps(data.get("ids", {})))
    finally:
        await pubsub.close()
        _connections_by_account[account_id] = max(
            0, _connections_by_account.get(account_id, 1) - 1
        )


async def _redis_or_none():
    url = getattr(settings, "REDIS_URL", "") or ""
    if not url.startswith(("redis://", "rediss://", "unix://")):
        return None
    try:
        import redis.asyncio as aredis

        client = aredis.from_url(url, socket_timeout=5, socket_connect_timeout=2)
        await client.ping()
        return client
    except Exception:
        logger.warning(
            "events.stream: Redis unavailable (%s) — heartbeat-only stream",
            url,
            exc_info=True,
        )
        return None

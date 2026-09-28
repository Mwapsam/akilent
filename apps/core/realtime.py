"""Publish side of the app's real-time updates (see apps/core/views_events.py for the other
half). One Redis pub/sub channel per account, so a subscriber only ever hears about the account
it's a member of.

Payloads carry ids and an event type only, never message text or other customer data — a
subscriber re-fetches the real content through the normal, permission-checked views (the inbox
list poll, the conversation feed), the same as if it noticed on its own via polling. This module
only tells it *when* to look sooner.

No-op wherever ``settings.REDIS_URL`` doesn't point at a real Redis (local dev without Redis,
tests): callers never need to check first, matching the pattern already used by
apps/email/services/rate_limiter.py for the SES send limiter.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_client = None
_client_checked = False


def _redis_client():
    """The shared sync Redis client for publishing, or None if unavailable.

    Publishing happens from ordinary request/Celery-task code (via ``on_commit``), not an async
    context, so this is the same sync ``redis`` client apps/email's rate limiter already uses —
    no need for ``redis.asyncio`` on this side.
    """
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True

    from django.conf import settings

    url = getattr(settings, "REDIS_URL", "") or ""
    if not url.startswith(("redis://", "rediss://", "unix://")):
        return None
    try:
        import redis

        client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        client.ping()
        _client = client
    except Exception:
        logger.warning(
            "realtime.publish: Redis unavailable (%s) — updates stay poll-only",
            url,
            exc_info=True,
        )
        _client = None
    return _client


def channel_name(account_id: int) -> str:
    return f"acct:{account_id}"


def publish(account_id: int, event: str, ids: dict | None = None) -> None:
    """Tell anyone subscribed to ``account_id`` that ``event`` happened, on commit.

    ``event`` is a short dotted name (``"conversation.updated"``, ``"message.created"``,
    ``"followup.due"``); ``ids`` is a small dict of primary/public ids the subscriber can use to
    decide what to re-fetch (e.g. ``{"conversation_id": "..."}``). Deferred to
    ``transaction.on_commit`` so a subscriber never gets nudged toward a row that isn't
    visible yet because the transaction that created it hasn't committed.
    """
    from django.db import transaction

    def _send():
        client = _redis_client()
        if client is None:
            return
        payload = json.dumps({"event": event, "ids": ids or {}})
        try:
            client.publish(channel_name(account_id), payload)
        except Exception:
            logger.warning(
                "realtime.publish: send failed for %s/%s",
                account_id,
                event,
                exc_info=True,
            )

    transaction.on_commit(_send)


def on_conversation_message_processed(
    sender, conversation, message, handled_by_automation, **kwargs
):
    """Connected in apps/core/apps.py to apps.conversations.signals.conversation_message_processed.

    Fires for every inbound *and* outbound message once deterministic automation has had its
    chance — the inbox list needs to know about both (a new inbound message changes who's
    waiting on whom; an outbound reply does too), while the open thread only needs to hear about
    the ones it doesn't already know about via its own optimistic send.
    """
    publish(
        conversation.account_id,
        "conversation.updated",
        {"conversation_id": conversation.public_id},
    )
    if message.direction == message.Direction.INBOUND:
        publish(
            conversation.account_id,
            "message.created",
            {"conversation_id": conversation.public_id},
        )

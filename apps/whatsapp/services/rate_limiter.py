"""Per-number outbound rate limiting for WhatsApp sends.

Reuses the token-bucket implementations from ``apps.email.services.rate_limiter``
(a distributed Redis bucket when ``settings.REDIS_URL`` is reachable, otherwise a
per-process fallback). One bucket per WhatsApp phone number id, so a fleet of
Celery workers together stays under Meta's throughput tier for that number.
"""

import logging

from django.conf import settings

from apps.email.services.rate_limiter import RedisTokenBucket, TokenBucket

logger = logging.getLogger(__name__)

_buckets: dict = {}


def _make_bucket(rate: float, key: str):
    url = getattr(settings, "REDIS_URL", "") or ""
    if url.startswith(("redis://", "rediss://", "unix://")):
        try:
            import redis

            client = redis.Redis.from_url(
                url, socket_timeout=2, socket_connect_timeout=2
            )
            client.ping()
            return RedisTokenBucket(client, rate=rate, key=key)
        except Exception:
            logger.warning(
                "WhatsApp rate limiter: Redis unavailable — per-process bucket",
                exc_info=True,
            )
    return TokenBucket(rate=rate)


def get_whatsapp_rate_limiter(phone_number_id: str, rate: float):
    """Return the process-wide limiter for one WhatsApp number, tracking `rate`."""
    rate = float(rate or 1)
    inst = _buckets.get(phone_number_id)
    if inst is None:
        inst = _make_bucket(rate, f"wa:ratelimit:{phone_number_id}")
        _buckets[phone_number_id] = inst
    elif inst.rate != rate:
        inst.rate = rate
        if isinstance(inst, TokenBucket):
            inst.capacity = rate
    return inst

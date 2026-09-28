"""Rate limiting for SES sends using a token-bucket.

Two backends, same interface (``acquire`` / ``wait_for``):

* ``RedisTokenBucket`` — a single bucket shared across every web/Celery worker
  via an atomic Lua script, so N workers together stay under the configured
  SES sends-per-second. Used whenever ``settings.REDIS_URL`` points at a
  reachable redis and the ``redis`` package is importable.
* ``TokenBucket`` — a per-process fallback for local dev / tests / a redis
  outage. With multiple workers the effective rate is N × the limit, so it is
  not safe for production multi-worker sending.

``get_ses_rate_limiter()`` picks the backend once and keeps the rate in sync
with ``MailProviderSettings.ses_send_rate_limit`` on every call.
"""

import logging
import time

logger = logging.getLogger(__name__)


class TokenBucket:
    """Per-process token bucket rate limiter for per-second flow control."""

    def __init__(self, rate: float, capacity: float | None = None):
        self.rate = rate
        self.capacity = capacity or rate
        self.tokens = self.capacity
        self.last_refill = time.time()

    def acquire(
        self, tokens: float = 1.0, blocking: bool = True, timeout: float = 5.0
    ) -> bool:
        start = time.time()
        while True:
            now = time.time()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens >= tokens:
                self.tokens -= tokens
                return True

            if not blocking:
                return False

            if time.time() - start >= timeout:
                logger.warning(
                    "TokenBucket.acquire timeout after %.1f sec (rate=%.1f/sec, need %.1f tokens)",
                    timeout,
                    self.rate,
                    tokens,
                )
                return False

            time.sleep(0.01)

    def wait_for(self, tokens: float = 1.0) -> float:
        """Blocking acquire with exponential backoff. Returns seconds waited."""
        start = time.time()
        retry_delay = 0.001
        max_delay = 1.0

        while not self.acquire(tokens, blocking=False):
            time.sleep(retry_delay)
            retry_delay = min(max_delay, retry_delay * 2)

        return time.time() - start


# Atomic token-bucket in Redis. Returns "0" when tokens were granted, otherwise
# the number of seconds to wait before enough tokens will have refilled.
_LUA_TOKEN_BUCKET = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

local delta = now - ts
if delta < 0 then delta = 0 end
tokens = math.min(capacity, tokens + delta * rate)

local allowed = tokens >= requested
if allowed then
  tokens = tokens - requested
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(capacity / rate) + 10)

if allowed then
  return '0'
end
return tostring((requested - tokens) / rate)
"""


class RedisTokenBucket:
    """Distributed token bucket backed by a single Redis key + Lua script."""

    def __init__(
        self,
        client,
        rate: float,
        capacity: float | None = None,
        key: str = "ses:ratelimit",
    ):
        self._client = client
        self.rate = rate
        self.capacity = capacity or rate
        self._key = key
        self._script = client.register_script(_LUA_TOKEN_BUCKET)

    def _wait_seconds(self, tokens: float) -> float:
        raw = self._script(
            keys=[self._key],
            args=[self.rate, self.capacity, time.time(), tokens],
        )
        return float(raw.decode() if isinstance(raw, bytes) else raw)

    def acquire(
        self, tokens: float = 1.0, blocking: bool = True, timeout: float = 5.0
    ) -> bool:
        start = time.time()
        while True:
            try:
                wait = self._wait_seconds(tokens)
            except Exception:
                # Redis hiccup: fail open rather than blocking the send pipeline.
                logger.exception("RedisTokenBucket: script failed; allowing send")
                return True

            if wait <= 0:
                return True
            if not blocking:
                return False
            if time.time() - start >= timeout:
                logger.warning(
                    "RedisTokenBucket.acquire timeout after %.1fs (rate=%.1f/s)",
                    timeout,
                    self.rate,
                )
                return False
            time.sleep(min(wait, 0.25))

    def wait_for(self, tokens: float = 1.0) -> float:
        start = time.time()
        while not self.acquire(tokens, blocking=False):
            time.sleep(0.01)
        return time.time() - start


def _make_redis_bucket(rate: float):
    from django.conf import settings

    url = getattr(settings, "REDIS_URL", "") or ""
    if not url.startswith(("redis://", "rediss://", "unix://")):
        return None
    try:
        import redis

        client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        client.ping()
        logger.info("SES rate limiter: using distributed Redis token bucket")
        return RedisTokenBucket(client, rate=rate)
    except Exception:
        logger.warning(
            "SES rate limiter: Redis unavailable (%s) — falling back to per-process bucket",
            url,
            exc_info=True,
        )
        return None


def get_ses_rate_limiter():
    """Return the process-wide SES rate limiter (Redis-backed when available)."""
    try:
        from apps.core.models import MailProviderSettings

        rate = MailProviderSettings.load().ses_send_rate_limit or 14
    except Exception as e:
        logger.warning(
            "Failed to load SES rate limit from settings: %s; using default 14/sec", e
        )
        rate = 14

    inst = getattr(get_ses_rate_limiter, "_instance", None)
    if inst is None:
        inst = _make_redis_bucket(rate) or TokenBucket(rate=rate)
        get_ses_rate_limiter._instance = inst  # type: ignore[attr-defined]
    elif inst.rate != rate:
        inst.rate = rate
        if isinstance(inst, TokenBucket):
            inst.capacity = rate
        logger.debug("Updated SES rate limit to %.1f/sec", rate)

    return get_ses_rate_limiter._instance  # type: ignore[attr-defined]

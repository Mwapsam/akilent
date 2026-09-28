"""SES send rate limiter — token-bucket math and backend selection."""

import time

from apps.email.services.rate_limiter import (
    RedisTokenBucket,
    TokenBucket,
    get_ses_rate_limiter,
)


def test_token_bucket_grants_up_to_capacity_then_throttles():
    tb = TokenBucket(rate=100, capacity=5)
    assert all(tb.acquire(blocking=False) for _ in range(5))
    assert tb.acquire(blocking=False) is False  # bucket drained


def test_token_bucket_refills_over_time():
    tb = TokenBucket(rate=50, capacity=1)  # 1 token / 20ms
    assert tb.acquire(blocking=False) is True
    assert tb.acquire(blocking=False) is False
    time.sleep(0.05)
    assert tb.acquire(blocking=False) is True


def test_token_bucket_blocking_times_out():
    tb = TokenBucket(rate=1, capacity=1)
    assert tb.acquire(blocking=False) is True
    start = time.time()
    assert tb.acquire(blocking=True, timeout=0.1) is False
    assert time.time() - start >= 0.1


def test_token_bucket_never_exceeds_capacity():
    tb = TokenBucket(rate=1000, capacity=3)
    time.sleep(0.05)  # would refill far past capacity if uncapped
    granted = sum(tb.acquire(blocking=False) for _ in range(10))
    assert granted == 3


class _FakeScript:
    """Stand-in for a registered Redis Lua script returning canned wait times."""

    def __init__(self, waits):
        self._waits = list(waits)

    def __call__(self, keys=None, args=None):
        return str(self._waits.pop(0)) if self._waits else "0"


def _redis_bucket(waits):
    tb = RedisTokenBucket.__new__(RedisTokenBucket)
    tb.rate = 10
    tb.capacity = 10
    tb._key = "test"
    tb._script = _FakeScript(waits)
    return tb


def test_redis_bucket_grants_when_script_returns_zero():
    tb = _redis_bucket(["0"])
    assert tb.acquire(blocking=False) is True


def test_redis_bucket_non_blocking_denies_when_wait_positive():
    tb = _redis_bucket(["0.5"])
    assert tb.acquire(blocking=False) is False


def test_redis_bucket_blocking_retries_until_granted():
    tb = _redis_bucket(["0.02", "0.02", "0"])
    start = time.time()
    assert tb.acquire(blocking=True, timeout=1.0) is True
    assert time.time() - start >= 0.03


def test_redis_bucket_fails_open_on_script_error():
    tb = RedisTokenBucket.__new__(RedisTokenBucket)
    tb.rate = tb.capacity = 10
    tb._key = "t"

    def _boom(**kw):
        raise RuntimeError("redis down")

    tb._script = _boom
    assert tb.acquire(blocking=False) is True  # fail open, don't block sends


def test_get_ses_rate_limiter_uses_in_process_fallback_without_redis(settings, db):
    settings.REDIS_URL = ""
    get_ses_rate_limiter._instance = None
    try:
        limiter = get_ses_rate_limiter()
        assert isinstance(limiter, TokenBucket)
    finally:
        get_ses_rate_limiter._instance = None


def test_get_ses_rate_limiter_rate_tracks_settings(settings, db, monkeypatch):
    settings.REDIS_URL = ""
    get_ses_rate_limiter._instance = None

    from apps.core.models import MailProviderSettings

    s = MailProviderSettings.load()
    s.ses_send_rate_limit = 7
    s.save()
    try:
        assert get_ses_rate_limiter().rate == 7
        s.ses_send_rate_limit = 21
        s.save()
        assert get_ses_rate_limiter().rate == 21
    finally:
        get_ses_rate_limiter._instance = None

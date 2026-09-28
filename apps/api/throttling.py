"""Per-API-key request-rate throttling.

Distinct from and complementary to apps.billing.limits.LimitChecker.check_email
(a monthly *volume* cap): this is a requests/minute *rate* limit, resolved
from the key's own override or its account's plan (Plan.api_rate_per_min).
"""

from __future__ import annotations

import time

from django.core.cache import cache
from rest_framework.throttling import BaseThrottle

_WINDOW_SECONDS = 60


class ApiKeyRateThrottle(BaseThrottle):
    def __init__(self):
        self._wait: int | None = None

    def _rate_for(self, api_key) -> int | None:
        if api_key.rate_per_min:
            return api_key.rate_per_min
        subscription = getattr(api_key.account, "subscription", None)
        plan = getattr(subscription, "plan", None) if subscription else None
        return getattr(plan, "api_rate_per_min", None)

    def allow_request(self, request, view) -> bool:
        api_key = getattr(request, "auth", None)
        if api_key is None:
            # No key resolved yet — authentication will reject the request;
            # nothing to throttle against.
            return True

        rate = self._rate_for(api_key)
        if not rate or rate <= 0:
            return True

        now = time.time()
        bucket = int(now // _WINDOW_SECONDS)
        cache_key = f"throttle:apikey:{api_key.pk}:{bucket}"
        cache.add(cache_key, 0, timeout=_WINDOW_SECONDS)
        count = cache.incr(cache_key)

        if count > rate:
            self._wait = _WINDOW_SECONDS - int(now % _WINDOW_SECONDS)
            return False
        return True

    def wait(self):
        return self._wait

from django.core.cache import cache

from apps.api.authentication import _lockout_cache_key


class _Req:
    META = {"REMOTE_ADDR": "203.0.113.7"}


def test_failed_attempts_from_earlier_test_are_not_visible_a():
    cache.set(_lockout_cache_key(_Req()), 99, timeout=600)


def test_failed_attempts_from_earlier_test_are_not_visible_b():
    assert cache.get(_lockout_cache_key(_Req()), 0) == 0

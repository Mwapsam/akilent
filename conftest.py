"""Pytest config. Test env + settings live in automator/settings_test.py
(referenced by DJANGO_SETTINGS_MODULE in pyproject.toml)."""

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _isolate_process_cache():
    """The test cache is a process-wide LocMemCache. Lockout counters and DNS/validation
    results written by one test otherwise leak into later tests (e.g. 403 lockouts keyed
    by the test client's IP). The channel on/off cache is process-wide too."""
    from apps.core import channels

    reset = {"value": None, "loaded_at": 0.0, "failed_at": 0.0}
    cache.clear()
    channels._cache.update(reset)
    yield
    cache.clear()
    channels._cache.update(reset)

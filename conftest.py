"""Pytest config. Test env + settings live in automator/settings_test.py
(referenced by DJANGO_SETTINGS_MODULE in pyproject.toml)."""

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _isolate_process_cache():
    """The test cache is a process-wide LocMemCache. Lockout counters and DNS/validation
    results written by one test otherwise leak into later tests (e.g. 403 lockouts keyed
    by the test client's IP)."""
    cache.clear()
    yield
    cache.clear()

import json

import pytest
from django.core.cache import cache

from apps.api.authentication import (
    _LOCKOUT_MAX_ATTEMPTS,
    _is_locked_out,
)

MESSAGES_URL = "/api/v1/messages"


class _FakeRequest:
    def __init__(self, ip: str):
        self.META = {"REMOTE_ADDR": ip}


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def test_not_locked_out_initially():
    req = _FakeRequest("10.0.0.1")
    assert _is_locked_out(req) is False


@pytest.mark.django_db
def test_repeated_bad_keys_get_locked_out(client):
    for _ in range(_LOCKOUT_MAX_ATTEMPTS):
        resp = client.post(
            MESSAGES_URL,
            data=json.dumps({"from": "a@example.com", "to": "b@example.com"}),
            content_type="application/json",
            HTTP_X_API_KEY="wrong-key",
        )
        assert resp.status_code == 401

    locked_out = client.post(
        MESSAGES_URL,
        data=json.dumps({"from": "a@example.com", "to": "b@example.com"}),
        content_type="application/json",
        HTTP_X_API_KEY="wrong-key",
    )
    assert locked_out.status_code == 429
    assert "Too many invalid API key attempts" in locked_out.json()["error"]["message"]


@pytest.mark.django_db
def test_missing_key_does_not_count_toward_lockout(client):
    for _ in range(_LOCKOUT_MAX_ATTEMPTS + 2):
        resp = client.post(
            MESSAGES_URL,
            data=json.dumps({"from": "a@example.com", "to": "b@example.com"}),
            content_type="application/json",
        )
        assert resp.status_code == 401

    # Missing-key requests never call _record_failed_attempt, so a real
    # (still invalid) key from the same client shouldn't be locked out yet.
    resp = client.post(
        MESSAGES_URL,
        data=json.dumps({"from": "a@example.com", "to": "b@example.com"}),
        content_type="application/json",
        HTTP_X_API_KEY="some-key",
    )
    assert resp.status_code == 401

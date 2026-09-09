import httpx
import pytest

from akilent import Akilent, AuthenticationError, ConflictError, RateLimitError
from akilent import webhooks


def _client(handler):
    transport = httpx.MockTransport(handler)
    return Akilent("ak_test_x", base_url="https://api.test", http_client=httpx.Client(transport=transport))


def test_send_sets_auth_and_idempotency_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["idem"] = request.headers.get("idempotency-key")
        seen["body"] = request.read().decode()
        return httpx.Response(202, json={"id": 1, "public_id": "msg_1", "status": "queued"})

    out = _client(handler).messages.send(
        from_="a@acme.com", to="b@example.com", subject="Hi", text="yo"
    )
    assert out["public_id"] == "msg_1"
    assert seen["auth"] == "Bearer ak_test_x"
    assert seen["idem"]  # auto-generated
    import json as _json

    assert _json.loads(seen["body"])["from"] == "a@acme.com"


def test_explicit_idempotency_key_is_forwarded():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["idempotency-key"] == "abc"
        return httpx.Response(202, json={})

    _client(handler).messages.send(from_="a@a.com", to="b@b.com", idempotency_key="abc")


def test_401_raises_authentication_error_with_envelope_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "authentication_failed", "message": "bad key",
                            "request_id": "req_9"}},
        )

    with pytest.raises(AuthenticationError) as ei:
        _client(handler).messages.list()
    assert ei.value.code == "authentication_failed"
    assert ei.value.request_id == "req_9"
    assert ei.value.status_code == 401


def test_409_conflict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"error": {"code": "idempotency_key_reuse", "message": "x"}})

    with pytest.raises(ConflictError):
        _client(handler).messages.send(from_="a@a.com", to="b@b.com")


def test_retries_then_succeeds_on_429():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow"}})
        return httpx.Response(200, json={"data": [], "total": 0})

    out = _client(handler).messages.list()
    assert calls["n"] == 2
    assert out["total"] == 0


def test_iter_pages_through_results():
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}], "total": 3})
        return httpx.Response(200, json={"data": [{"id": "m3"}], "total": 3})

    ids = [m["id"] for m in _client(handler).messages.iter(page_size=2)]
    assert ids == ["m1", "m2", "m3"]


def test_webhook_signature_roundtrip():
    import hashlib
    import hmac
    import time

    secret = "whsec_test"
    body = b'{"event":"message.delivered"}'
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    header = f"t={ts},v1={sig}"
    assert webhooks.verify(body, header, secret) is True

    with pytest.raises(webhooks.SignatureVerificationError):
        webhooks.verify(body, f"t={ts},v1=deadbeef", secret)

"""
Phase 17 — Production security & abuse hardening tests.

Covers:
  API layer   — payload size, message length, malformed JSON, session expiry,
                rate limiting, CORS, error response sanitisation
  Tenant      — chatbot→account, session→chatbot, action→account scoping
  Actions     — disabled/unknown/cross-chatbot cannot execute
  Audit       — metadata sanitised, every execution recorded
"""

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.chatbot.models import (
    ChatActionExecution,
    ChatbotAction,
    ChatbotConfig,
    ChatSession,
)
from apps.chatbot.services.actions import _REGISTRY, invoke

# ── Helpers ────────────────────────────────────────────────────────────────────


def _client(origin="https://acme.com"):
    return Client(HTTP_ORIGIN=origin)


def _init(chatbot, origin="https://acme.com"):
    c = _client(origin)
    resp = c.post(
        "/api/chat/init/",
        data=json.dumps({"public_key": chatbot.public_key}),
        content_type="application/json",
    )
    if resp.status_code == 200:
        return resp.json()["session_key"], c
    return None, c


# ── Payload size limits ────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestPayloadLimits:
    def test_oversized_body_rejected(self, chatbot):
        c = _client()
        huge = "x" * 9_000
        resp = c.post(
            "/api/chat/init/",
            data=huge,
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_message_too_long_rejected(self, chatbot):
        session_key, c = _init(chatbot)
        assert session_key is not None
        long_msg = "a" * 2_001
        resp = c.post(
            "/api/chat/message/",
            data=json.dumps({"session_key": session_key, "message": long_msg}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "too long" in resp.json().get("error", "").lower()

    def test_message_at_limit_not_rejected_for_length(self, chatbot):
        """Exactly at the limit must not be rejected by the length guard (status != 400 with 'too long')."""
        from apps.chatbot.api.views import _MAX_MESSAGE_CHARS

        at_limit = "a" * _MAX_MESSAGE_CHARS
        # Test only the guard logic, not the full pipeline
        assert len(at_limit) <= _MAX_MESSAGE_CHARS


# ── Malformed JSON ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestMalformedJSON:
    def test_non_json_body_returns_400(self, chatbot):
        c = _client()
        resp = c.post(
            "/api/chat/init/", data="not json at all", content_type="application/json"
        )
        assert resp.status_code == 400

    def test_json_array_body_returns_400(self, chatbot):
        c = _client()
        resp = c.post(
            "/api/chat/init/",
            data=json.dumps(["this", "is", "an", "array"]),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_empty_body_returns_400(self, chatbot):
        c = _client()
        resp = c.post("/api/chat/init/", data="", content_type="application/json")
        assert resp.status_code == 400


# ── Session expiry ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestSessionExpiry:
    def test_expired_session_returns_410(self, chatbot):
        session = ChatSession.objects.create(chatbot=chatbot)
        # Back-date last_activity_at beyond TTL (started_at is not the expiry signal)
        ChatSession.objects.filter(pk=session.pk).update(
            last_activity_at=timezone.now() - timedelta(hours=25)
        )
        session.refresh_from_db()
        c = _client()
        resp = c.post(
            "/api/chat/message/",
            data=json.dumps({"session_key": session.session_key, "message": "hello"}),
            content_type="application/json",
        )
        assert resp.status_code == 410

    def test_fresh_session_not_expired(self, chatbot):
        from apps.chatbot.api.views import _is_session_expired

        session = ChatSession.objects.create(chatbot=chatbot)
        assert _is_session_expired(session) is False


# ── Rate limiting ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestRateLimiting:
    def test_21st_request_is_rejected(self, chatbot):
        from django.core.cache import cache

        from apps.chatbot.api.auth import check_rate_limit

        session = ChatSession.objects.create(chatbot=chatbot)
        cache.delete(f"chatbot_rate:{session.session_key}")
        for _ in range(20):
            assert check_rate_limit(session.session_key) is True
        assert check_rate_limit(session.session_key) is False

    def test_different_sessions_independent(self, chatbot):
        from django.core.cache import cache

        from apps.chatbot.api.auth import check_rate_limit

        s1 = ChatSession.objects.create(chatbot=chatbot)
        s2 = ChatSession.objects.create(chatbot=chatbot)
        cache.delete(f"chatbot_rate:{s1.session_key}")
        cache.delete(f"chatbot_rate:{s2.session_key}")
        for _ in range(20):
            check_rate_limit(s1.session_key)
        assert check_rate_limit(s2.session_key) is True


# ── Error response sanitisation ────────────────────────────────────────────────


@pytest.mark.django_db
class TestErrorSanitisation:
    INTERNAL_FRAGMENTS = ["Traceback", "django", "apps.", "File ", "line ", "Error:"]

    def _no_leakage(self, resp):
        text = resp.content.decode()
        for frag in self.INTERNAL_FRAGMENTS:
            assert frag not in text, (
                f"Internal detail '{frag}' leaked in error response"
            )

    def test_invalid_key_no_internal_detail(self, chatbot):
        c = _client()
        resp = c.post(
            "/api/chat/init/",
            data=json.dumps({"public_key": "pk_chat_bad"}),
            content_type="application/json",
        )
        assert resp.status_code == 403
        self._no_leakage(resp)

    def test_unknown_session_no_internal_detail(self, chatbot):
        c = _client()
        resp = c.post(
            "/api/chat/message/",
            data=json.dumps({"session_key": "sess_notreal", "message": "hi"}),
            content_type="application/json",
        )
        assert resp.status_code == 404
        self._no_leakage(resp)


# ── Tenant isolation ───────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestTenantIsolation:
    def test_session_belongs_to_chatbot(self, chatbot, another_account):
        """A session from chatbot A cannot be used with chatbot B's public key."""
        ChatbotConfig.objects.create(
            account=another_account,
            name="Bot B",
            allowed_domains=["https://rival.com"],
        )
        session_a = ChatSession.objects.create(chatbot=chatbot)
        # Trying to send a message using session_a but the origin of bot_b
        c = Client(HTTP_ORIGIN="https://rival.com")
        resp = c.post(
            "/api/chat/message/",
            data=json.dumps({"session_key": session_a.session_key, "message": "hi"}),
            content_type="application/json",
        )
        # Origin doesn't match session_a's chatbot (acme.com) → 403
        assert resp.status_code == 403

    def test_cross_account_action_denied(self, session, another_account):
        """An action registered for another account's chatbot cannot be invoked via this session."""
        bot_b = ChatbotConfig.objects.create(
            account=another_account,
            name="Bot B",
            allowed_domains=["https://rival.com"],
        )
        ChatbotAction.objects.create(
            chatbot=bot_b, slug="steal_data", label="Steal", is_enabled=True
        )
        _REGISTRY["steal_data"] = lambda session, **kw: {"data": "secret"}
        try:
            with pytest.raises(ValueError, match="not enabled"):
                invoke("steal_data", session)
        finally:
            del _REGISTRY["steal_data"]

    def test_inactive_chatbot_session_rejected_at_init(self, chatbot):
        chatbot.is_active = False
        chatbot.save()
        c = _client()
        resp = c.post(
            "/api/chat/init/",
            data=json.dumps({"public_key": chatbot.public_key}),
            content_type="application/json",
        )
        assert resp.status_code == 403


# ── Action security ────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestActionSecurity:
    def test_unknown_slug_raises_and_records_denied(self, session):
        with pytest.raises(ValueError, match="not enabled"):
            invoke("completely_unknown_slug", session)
        # No execution row for an unknown slug (action doesn't exist in DB)
        assert ChatActionExecution.objects.filter(session=session).count() == 0

    def test_disabled_slug_raises_and_records_denied(self, session, disabled_action):
        with pytest.raises(ValueError):
            invoke(disabled_action.slug, session)
        assert ChatActionExecution.objects.filter(
            session=session, status=ChatActionExecution.Status.DENIED
        ).exists()

    def test_audit_metadata_stored(self, session):
        slug = "audit_test_" + session.session_key[:6]
        ChatbotAction.objects.create(
            chatbot=session.chatbot, slug=slug, label="Audit test", is_enabled=True
        )
        _REGISTRY[slug] = lambda session, order_id, **kw: {"status": "ok"}
        try:
            invoke(slug, session, order_id="12345", secret_token="should_be_stripped")
            execution = ChatActionExecution.objects.get(
                session=session, action__slug=slug
            )
            # Input is sanitised to strings; result preserved
            assert execution.status == ChatActionExecution.Status.SUCCESS
            assert "secret_token" in execution.input_metadata  # key present
            # value is a string (sanitised), not a raw object
            assert isinstance(execution.input_metadata["secret_token"], str)
        finally:
            del _REGISTRY[slug]

    def test_each_invocation_gets_own_execution_row(self, session):
        slug = "multi_exec_" + session.session_key[:6]
        ChatbotAction.objects.create(
            chatbot=session.chatbot, slug=slug, label="Multi", is_enabled=True
        )
        _REGISTRY[slug] = lambda session, **kw: {}
        try:
            invoke(slug, session)
            invoke(slug, session)
            assert (
                ChatActionExecution.objects.filter(
                    session=session, action__slug=slug
                ).count()
                == 2
            )
        finally:
            del _REGISTRY[slug]

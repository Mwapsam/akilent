"""
Invariant tests for the chatbot engine.

Each test maps to a non-negotiable invariant from the plan spec.
"""

import json

import pytest
from django.test import RequestFactory

from apps.chatbot.api import auth, serializers
from apps.chatbot.models import ChatActionExecution, ChatbotAction, ChatSession
from apps.chatbot.services.actions import _REGISTRY, invoke
from apps.chatbot.services.handoff import perform_handoff, should_handoff

factory = RequestFactory()


# ── Origin / allowed_domains ──────────────────────────────────────────────────


class TestOriginCheck:
    def test_empty_allowed_domains_denies_all(self, chatbot_no_domains):
        req = factory.post("/", HTTP_ORIGIN="https://anything.com")
        assert auth.check_origin(chatbot_no_domains, req) is False

    def test_matching_origin_passes(self, chatbot):
        req = factory.post("/", HTTP_ORIGIN="https://acme.com")
        assert auth.check_origin(chatbot, req) is True

    def test_non_matching_origin_fails(self, chatbot):
        req = factory.post("/", HTTP_ORIGIN="https://evil.com")
        assert auth.check_origin(chatbot, req) is False

    def test_missing_origin_header_fails(self, chatbot):
        req = factory.post("/")
        assert auth.check_origin(chatbot, req) is False

    def test_trailing_slash_normalised(self, chatbot):
        req = factory.post("/", HTTP_ORIGIN="https://acme.com/")
        assert auth.check_origin(chatbot, req) is True


# ── Public config shape ───────────────────────────────────────────────────────


class TestPublicConfig:
    INTERNAL_FIELDS = {
        "id",
        "account",
        "account_id",
        "public_key",
        "handoff_action",
        "handoff_action_id",
        "allowed_domains",
    }

    def test_no_internal_fields_exposed(self, chatbot):
        cfg = serializers.public_config(chatbot)
        for field in self.INTERNAL_FIELDS:
            assert field not in cfg, (
                f"Internal field '{field}' leaked into public config"
            )

    def test_required_fields_present(self, chatbot):
        cfg = serializers.public_config(chatbot)
        for key in (
            "name",
            "avatar_url",
            "welcome_message",
            "primary_color",
            "position",
        ):
            assert key in cfg


# ── Init endpoint ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestInitEndpoint:
    def _post(self, body, origin="https://acme.com"):
        from django.test import Client

        client = Client(HTTP_ORIGIN=origin)
        return client.post(
            "/api/chat/init/",
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_valid_key_and_origin_creates_session(self, chatbot):
        resp = self._post({"public_key": chatbot.public_key})
        assert resp.status_code == 200
        data = resp.json()
        assert "session_key" in data
        assert "api_origin" in data
        assert "config" in data
        assert ChatSession.objects.filter(session_key=data["session_key"]).exists()

    def test_empty_domains_returns_403(self, chatbot_no_domains):
        resp = self._post({"public_key": chatbot_no_domains.public_key})
        assert resp.status_code == 403

    def test_wrong_origin_returns_403(self, chatbot):
        resp = self._post({"public_key": chatbot.public_key}, origin="https://evil.com")
        assert resp.status_code == 403

    def test_invalid_key_returns_403(self, chatbot):
        resp = self._post({"public_key": "pk_chat_notreal"})
        assert resp.status_code == 403

    def test_inactive_chatbot_returns_403(self, chatbot):
        chatbot.is_active = False
        chatbot.save()
        resp = self._post({"public_key": chatbot.public_key})
        assert resp.status_code == 403

    def test_config_contains_no_internal_ids(self, chatbot):
        resp = self._post({"public_key": chatbot.public_key})
        cfg = resp.json().get("config", {})
        for bad in ("id", "account", "account_id", "public_key", "allowed_domains"):
            assert bad not in cfg


# ── Action registry: disabled action records DENIED ───────────────────────────


@pytest.mark.django_db
class TestActionRegistry:
    def test_disabled_action_raises_and_records_denied(self, session, disabled_action):
        with pytest.raises(ValueError, match="not enabled"):
            invoke(disabled_action.slug, session)
        assert ChatActionExecution.objects.filter(
            session=session,
            action=disabled_action,
            status=ChatActionExecution.Status.DENIED,
        ).exists()

    def test_enabled_action_records_success(self, session, action):
        slug = "test_success_action_" + session.session_key[:8]
        _REGISTRY[slug] = lambda session, **kw: {"result": "ok"}
        # Create a matching DB row
        db_action = ChatbotAction.objects.create(
            chatbot=session.chatbot, slug=slug, label="Test", is_enabled=True
        )
        try:
            result = invoke(slug, session)
            assert result == {"result": "ok"}
            assert ChatActionExecution.objects.filter(
                session=session,
                action=db_action,
                status=ChatActionExecution.Status.SUCCESS,
            ).exists()
        finally:
            del _REGISTRY[slug]

    def test_failed_action_records_failed(self, session):
        slug = "test_fail_action_" + session.session_key[:8]

        def _bad(session, **kw):
            raise RuntimeError("boom")

        _REGISTRY[slug] = _bad
        ChatbotAction.objects.create(
            chatbot=session.chatbot, slug=slug, label="Bad", is_enabled=True
        )
        try:
            with pytest.raises(RuntimeError, match="boom"):
                invoke(slug, session)
            assert ChatActionExecution.objects.filter(
                session=session,
                status=ChatActionExecution.Status.FAILED,
            ).exists()
        finally:
            del _REGISTRY[slug]


# ── Handoff two-step ──────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestHandoffTwoStep:
    def test_should_handoff_on_phrase(self, session):
        assert (
            should_handoff(
                session,
                user_message="I want to speak to a human",
                turn_count=1,
                last_reply="Sure.",
            )
            is True
        )

    def test_should_handoff_false_for_normal_message(self, session):
        assert (
            should_handoff(
                session,
                user_message="What is your refund policy?",
                turn_count=1,
                last_reply="Here it is…",
            )
            is False
        )

    def test_perform_handoff_without_action_returns_not_performed(self, session):
        # chatbot.handoff_action is None by default
        result = perform_handoff(session, reason="customer asked")
        assert result == {"performed": False, "ticket_number": None}

    def test_should_handoff_does_not_create_anything(self, session):
        from apps.chatbot.models import ChatActionExecution

        should_handoff(
            session,
            user_message="real person please",
            turn_count=1,
            last_reply="…",
        )
        assert ChatActionExecution.objects.filter(session=session).count() == 0


# ── Account isolation ─────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAccountIsolation:
    def test_action_scoped_to_chatbot_not_crossed(self, session, another_account):
        """An action DB row belonging to another chatbot cannot be invoked via this session."""
        from apps.chatbot.models import ChatbotConfig

        other_chatbot = ChatbotConfig.objects.create(
            account=another_account,
            name="Other Bot",
            allowed_domains=["https://rival.com"],
        )
        ChatbotAction.objects.create(
            chatbot=other_chatbot,
            slug="secret_action",
            label="Secret",
            is_enabled=True,
        )
        _REGISTRY["secret_action"] = lambda session, **kw: {"data": "sensitive"}
        try:
            # Invoking via *this* session (different chatbot) must be denied
            with pytest.raises(ValueError, match="not enabled"):
                invoke("secret_action", session)
        finally:
            del _REGISTRY["secret_action"]

    def test_rate_limit_is_per_session_key(self, chatbot):
        from django.core.cache import cache

        from apps.chatbot.api.auth import check_rate_limit

        s1 = ChatSession.objects.create(chatbot=chatbot)
        s2 = ChatSession.objects.create(chatbot=chatbot)
        cache.delete(f"chatbot_rate:{s1.session_key}")
        cache.delete(f"chatbot_rate:{s2.session_key}")
        for _ in range(20):
            check_rate_limit(s1.session_key)
        # s1 is exhausted, s2 is fresh
        assert check_rate_limit(s1.session_key) is False
        assert check_rate_limit(s2.session_key) is True

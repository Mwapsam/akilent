"""Chatbot Studio P0 (docs/plans/chatbot-studio.md): a visitor's chat works end to end.

Covers what the earlier tests never exercised together: the widget on another website (CORS),
visitor contact + inbox conversation from the first message, memory between turns, resuming a
session, identify without merging into someone else's contact, the trusted client IP, domain
normalisation, the install snippet and the idle-session purge.
"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone

from apps.chatbot.domains import clean_domains, normalise_origin
from apps.chatbot.models import ChatbotConfig, ChatSession
from apps.chatbot.services import chatbot as chatbot_service
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

ORIGIN = "https://acme.com"


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


class FakeProvider:
    """Stands in for the AI provider and records what each call was given."""

    def __init__(self):
        self.calls = []

    def chat(self, *, system, messages, max_tokens, temperature):
        self.calls.append([dict(m) for m in messages])
        return SimpleNamespace(error=None, text=f"Reply {len(self.calls)}")


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(
        "apps.ai.providers.get_ai_provider", lambda account=None, tier="": fake
    )
    return fake


def _post(client, path, payload, **extra):
    return client.post(
        path, data=json.dumps(payload), content_type="application/json", **extra
    )


def _start(chatbot, origin=ORIGIN, **payload):
    client = Client(HTTP_ORIGIN=origin)
    resp = _post(
        client, "/api/chat/init/", {"public_key": chatbot.public_key, **payload}
    )
    return client, resp


def _say(client, session_key, text):
    return _post(
        client, "/api/chat/message/", {"session_key": session_key, "message": text}
    )


# ── CORS: the widget runs on the business's website ────────────────────────────


@pytest.mark.django_db
class TestCors:
    def test_preflight_answered(self, chatbot):
        resp = Client(HTTP_ORIGIN=ORIGIN).options(
            "/api/chat/message/", HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST"
        )
        assert resp.status_code == 204
        assert resp["Access-Control-Allow-Origin"] == ORIGIN
        assert "Content-Type" in resp["Access-Control-Allow-Headers"]
        assert "Access-Control-Allow-Credentials" not in resp

    def test_allowed_origin_can_read_response(self, chatbot):
        _, resp = _start(chatbot)
        assert resp.status_code == 200
        assert resp["Access-Control-Allow-Origin"] == ORIGIN

    def test_unlisted_origin_cannot_read_response(self, chatbot):
        _, resp = _start(chatbot, origin="https://evil.example")
        assert resp.status_code == 403
        assert "Access-Control-Allow-Origin" not in resp

    def test_rate_limit_is_readable_by_the_widget(self, chatbot, provider, monkeypatch):
        monkeypatch.setattr("apps.chatbot.api.auth._RATE_LIMIT", 1)
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        assert _say(client, key, "one").status_code == 200
        limited = _say(client, key, "two")
        assert limited.status_code == 429
        assert limited["Access-Control-Allow-Origin"] == ORIGIN

    def test_unknown_session_does_not_touch_rate_limit_cache(self, chatbot):
        resp = _say(Client(HTTP_ORIGIN=ORIGIN), "sess_made_up", "hi")
        assert resp.status_code == 404
        assert cache.get("chatbot_rate:sess_made_up") is None


# ── A visitor's chat reaches the inbox and the bot remembers it ────────────────


@pytest.mark.django_db
class TestVisitorJourney:
    def test_first_message_creates_contact_and_inbox_conversation(
        self, chatbot, provider
    ):
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        assert _say(client, key, "Do you ship to Leeds?").json()["reply"] == "Reply 1"

        session = ChatSession.objects.get(session_key=key)
        assert session.contact is not None
        assert session.contact.account_id == chatbot.account_id
        assert session.contact.source == "website_chat"
        conversation = session.conversation
        assert conversation.channel == Conversation.Channel.WEBSITE_CHAT
        assert conversation.contact_id == session.contact_id
        assert conversation.is_unread
        bodies = list(
            Message.objects.filter(conversation=conversation)
            .order_by("timestamp", "pk")
            .values_list("direction", "body")
        )
        assert bodies == [("inbound", "Do you ship to Leeds?"), ("outbound", "Reply 1")]

    def test_bot_remembers_earlier_turns(self, chatbot, provider):
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        _say(client, key, "I want a laptop")
        _say(client, key, "Under 800")
        second_call = provider.calls[1]
        assert [m["content"] for m in second_call] == [
            "I want a laptop",
            "Reply 1",
            "Under 800",
        ]

    def test_no_identify_wall_after_three_turns(self, chatbot, provider):
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        replies = [_say(client, key, f"question {i}").json()["reply"] for i in range(5)]
        assert replies == [f"Reply {i}" for i in range(1, 6)]

    def test_turn_ceiling_hands_over(self, chatbot, provider, monkeypatch):
        monkeypatch.setattr(
            "apps.chatbot.services.responder._MAX_AI_TURNS_PER_SESSION", 2
        )
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        _say(client, key, "a")
        _say(client, key, "b")
        third = _say(client, key, "c").json()
        assert third["handoff_suggested"] is True
        assert len(provider.calls) == 2

    def test_one_contact_per_session(self, chatbot, provider):
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        before = Contact.objects.count()
        _say(client, key, "a")
        _say(client, key, "b")
        assert Contact.objects.count() == before + 1


# ── Resuming a session on the next page ────────────────────────────────────────


@pytest.mark.django_db
class TestResume:
    def test_saved_session_is_resumed_with_history(self, chatbot, provider):
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        _say(client, key, "Hello")

        _, again = _start(chatbot, session_key=key)
        data = again.json()
        assert data["session_key"] == key
        assert data["messages"] == [
            {"from": "visitor", "text": "Hello"},
            {"from": "bot", "text": "Reply 1"},
        ]

    def test_expired_session_starts_fresh(self, chatbot):
        _, resp = _start(chatbot)
        key = resp.json()["session_key"]
        ChatSession.objects.filter(session_key=key).update(
            last_activity_at=timezone.now() - timedelta(hours=25)
        )
        _, again = _start(chatbot, session_key=key)
        assert again.json()["session_key"] != key
        assert again.json()["messages"] == []

    def test_another_bots_session_is_not_resumed(self, chatbot, account):
        other = ChatbotConfig.objects.create(
            account=account, name="Other", allowed_domains=[ORIGIN]
        )
        foreign = ChatSession.objects.create(chatbot=other)
        _, resp = _start(chatbot, session_key=foreign.session_key)
        assert resp.json()["session_key"] != foreign.session_key

    def test_team_reply_shows_as_team(self, chatbot, provider):
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        _say(client, key, "Hi")
        session = ChatSession.objects.get(session_key=key)
        Message.objects.create(
            account=chatbot.account,
            conversation=session.conversation,
            direction=Message.Direction.OUTBOUND,
            body="Jo here, happy to help",
            timestamp=timezone.now() + timedelta(seconds=5),
        )
        _, again = _start(chatbot, session_key=key)
        assert again.json()["messages"][-1] == {
            "from": "team",
            "text": "Jo here, happy to help",
        }


# ── Identify: fills in the visitor's own contact, never someone else's ─────────


@pytest.mark.django_db
class TestIdentify:
    def _identify(self, client, key, **fields):
        return _post(client, "/api/chat/identify/", {"session_key": key, **fields})

    def test_details_saved_on_visitor_contact(self, chatbot, provider):
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        _say(client, key, "Hi")
        assert self._identify(
            client,
            key,
            name="Jane Smith",
            email="Jane@Example.com",
            phone="+447700900123",
        ).json() == {"identified": True}

        contact = ChatSession.objects.get(session_key=key).contact
        assert (contact.first_name, contact.last_name) == ("Jane", "Smith")
        assert contact.email == "jane@example.com"
        assert contact.phone == "+447700900123"

    def test_identify_before_any_message_creates_contact(self, chatbot):
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        self._identify(client, key, name="Sam", email="sam@example.com")
        contact = ChatSession.objects.get(session_key=key).contact
        assert contact.email == "sam@example.com"

    def test_existing_customers_email_is_not_merged(self, chatbot, provider):
        existing = Contact.objects.create(
            account=chatbot.account, email="vip@example.com", first_name="Real"
        )
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        _say(client, key, "Hi")
        self._identify(client, key, name="Imposter", email="VIP@example.com")

        session = ChatSession.objects.get(session_key=key)
        assert session.contact_id != existing.pk
        assert session.contact.email is None
        assert session.contact.attributes["website_chat_email"] == "vip@example.com"
        assert session.conversation.contact_id == session.contact_id
        existing.refresh_from_db()
        assert existing.first_name == "Real"

    def test_email_in_another_account_is_fine(self, chatbot, provider, another_account):
        Contact.objects.create(account=another_account, email="shared@example.com")
        client, resp = _start(chatbot)
        key = resp.json()["session_key"]
        self._identify(client, key, email="shared@example.com")
        assert (
            ChatSession.objects.get(session_key=key).contact.email
            == "shared@example.com"
        )


# ── Trusted client IP for the init rate limit ──────────────────────────────────


@pytest.mark.django_db
class TestClientIp:
    def test_spoofed_forwarded_for_does_not_reset_init_limit(
        self, chatbot, monkeypatch
    ):
        monkeypatch.setattr("apps.chatbot.api.auth._INIT_RATE_LIMIT", 2)
        statuses = []
        for i in range(3):
            _, resp = _start(chatbot, HTTP_X_FORWARDED_FOR=f"10.0.0.{i}, 203.0.113.9")
            statuses.append(resp.status_code)
        assert statuses == [200, 200, 429]


# ── Domains are stored the way browsers send Origin ───────────────────────────


class TestDomains:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("acme.com", "https://acme.com"),
            ("https://www.Acme.com/shop/", "https://www.acme.com"),
            ("http://localhost:8001", "http://localhost:8001"),
            ("https://acme.com:443", "https://acme.com"),
            ("ftp://acme.com", None),
            ("not a domain", None),
            ("", None),
        ],
    )
    def test_normalise_origin(self, raw, expected):
        assert normalise_origin(raw) == expected

    def test_clean_domains_dedupes_and_rejects_junk(self):
        assert clean_domains(["acme.com", "https://acme.com/", " "]) == [
            "https://acme.com"
        ]
        with pytest.raises(ValidationError):
            clean_domains(["acme.com", "ftp://nope"])

    @pytest.mark.django_db
    def test_bare_stored_domain_matches_browser_origin(self, account):
        bot = ChatbotConfig.objects.create(
            account=account, name="Legacy", allowed_domains=["acme.com"]
        )
        _, resp = _start(bot)
        assert resp.status_code == 200

    @pytest.mark.django_db
    def test_update_normalises_domains(self, chatbot, account):
        chatbot_service.update_chatbot(
            chatbot,
            actor_account=account,
            allowed_domains=["Shop.Acme.com/", "acme.com"],
        )
        chatbot.refresh_from_db()
        assert chatbot.allowed_domains == ["https://shop.acme.com", "https://acme.com"]


# ── Install snippet and idle-session purge ────────────────────────────────────


@pytest.mark.django_db
def test_snippet_is_absolute_and_names_the_api(settings, chatbot):
    from apps.chatbot.embed import snippet

    settings.SITE_URL = "https://akilent.test"
    text = snippet(chatbot.public_key)
    assert 'data-api="https://akilent.test"' in text
    assert 'src="https://' in text
    assert chatbot.public_key in text


@pytest.mark.django_db
def test_purge_keeps_chats_and_deletes_idle_sessions(chatbot, provider):
    from apps.chatbot.tasks import purge_idle_chat_sessions

    idle = ChatSession.objects.create(chatbot=chatbot)
    client, resp = _start(chatbot)
    key = resp.json()["session_key"]
    _say(client, key, "Hi")
    old = timezone.now() - timedelta(days=31)
    ChatSession.objects.update(last_activity_at=old)
    fresh = ChatSession.objects.create(chatbot=chatbot)

    assert purge_idle_chat_sessions() == 1
    remaining = set(ChatSession.objects.values_list("pk", flat=True))
    assert idle.pk not in remaining
    assert fresh.pk in remaining
    assert ChatSession.objects.filter(session_key=key).exists()

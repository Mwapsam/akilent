"""Chatbot Studio P1 (docs/plans/chatbot-studio.md): website chat as a channel.

The bot runs its own automation (website-only triggers), a waiting automation or the team come
before the AI, the team's inbox replies reach the widget through /api/chat/poll/, and the
account's WhatsApp/Instagram/Facebook message automations never answer a website visitor.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client

from apps.accounts.models import Membership
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import validate_definition
from apps.chatbot.models import ChatbotConfig, ChatSession
from apps.conversations.models import Conversation, Message

ORIGIN = "https://acme.com"
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


class FakeProvider:
    def __init__(self):
        self.calls = 0

    def grounded_answer(self, conversation, *, operation_id):
        self.calls += 1
        return {"ok": True, "text": f"AI answer {self.calls}", "failed": ""}


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr("apps.ai.api.grounded_answer", fake.grounded_answer)
    monkeypatch.setattr("apps.ai.api.is_available", lambda account: True)
    return fake


def _workflow(account, trigger, steps, name="Website bot", **extra):
    return Workflow.objects.create(
        account=account,
        name=name,
        slug=name.lower().replace(" ", "-"),
        status=Workflow.Status.PUBLISHED,
        definition={"trigger": trigger, "steps": steps},
        **extra,
    )


REPLY_STEPS = [
    {"id": "r", "type": "reply_text", "text": "Hi from the flow!", "next": "stop"},
    {"id": "stop", "type": "stop"},
]


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def _start(chatbot):
    client = Client(HTTP_ORIGIN=ORIGIN)
    data = _post(client, "/api/chat/init/", {"public_key": chatbot.public_key}).json()
    return client, data["session_key"]


def _say(client, key, text):
    return _post(client, "/api/chat/message/", {"session_key": key, "message": text})


def _poll(client, key, cursor):
    return _post(client, "/api/chat/poll/", {"session_key": key, "cursor": cursor})


def _conversation(key):
    return ChatSession.objects.get(session_key=key).conversation


# ── The bot's own automation ────────────────────────────────────────────────────


class TestBotWorkflow:
    def test_bot_workflow_answers_instead_of_ai(self, chatbot, account, provider):
        chatbot.workflow = _workflow(
            account, {"type": "website_chat.message_received"}, REPLY_STEPS
        )
        chatbot.save()
        client, key = _start(chatbot)
        data = _say(client, key, "hello").json()

        assert data["messages"] == [{"from": "bot", "text": "Hi from the flow!"}]
        assert data["reply"] == "Hi from the flow!"
        assert provider.calls == 0
        inbound = Message.objects.get(
            conversation=_conversation(key), direction="inbound"
        )
        assert inbound.metadata["handled_by"] == "automation"

    def test_keyword_miss_falls_back_to_ai(self, chatbot, account, provider):
        chatbot.workflow = _workflow(
            account,
            {"type": "website_chat.message_received", "match": {"any": ["price"]}},
            REPLY_STEPS,
        )
        chatbot.save()
        client, key = _start(chatbot)
        data = _say(client, key, "do you deliver?").json()
        assert data["messages"] == [{"from": "bot", "text": "AI answer 1"}]

    def test_draft_workflow_does_not_run(self, chatbot, account, provider):
        wf = _workflow(account, {"type": "website_chat.message_received"}, REPLY_STEPS)
        chatbot.workflow = wf
        chatbot.save()
        Workflow.objects.filter(pk=wf.pk).update(status=Workflow.Status.DRAFT)
        client, key = _start(chatbot)
        assert _say(client, key, "hi").json()["reply"] == "AI answer 1"

    def test_account_message_workflows_never_answer_website_chat(
        self, chatbot, account, provider
    ):
        other = _workflow(
            account,
            {"type": "conversation.message_received"},
            REPLY_STEPS,
            name="WhatsApp greeting",
        )
        client, key = _start(chatbot)
        data = _say(client, key, "hi").json()
        assert data["reply"] == "AI answer 1"
        assert not WorkflowRun.objects.filter(workflow=other).exists()

    def test_website_workflow_never_fires_on_other_channels(self, account):
        from apps.automation.workflow_engine import answer_message
        from apps.contacts.models import Contact

        wf = _workflow(account, {"type": "website_chat.message_received"}, REPLY_STEPS)
        contact = Contact.objects.create(account=account, phone="+447700900000")
        answer_message(
            account.id, contact, context={"message": {"body": "hi", "type": "text"}}
        )
        assert not WorkflowRun.objects.filter(workflow=wf).exists()


class TestOpened:
    def test_opened_workflow_greets_once(self, chatbot, account, provider):
        chatbot.workflow = _workflow(
            account,
            {"type": "website_chat.opened"},
            [
                {
                    "id": "r",
                    "type": "reply_text",
                    "text": "Welcome! How can we help?",
                    "next": "stop",
                },
                {"id": "stop", "type": "stop"},
            ],
        )
        chatbot.save()
        client, key = _start(chatbot)
        first = _post(client, "/api/chat/open/", {"session_key": key}).json()
        assert first["messages"] == [
            {"from": "bot", "text": "Welcome! How can we help?"}
        ]
        assert first["cursor"] == 1
        again = _post(client, "/api/chat/open/", {"session_key": key}).json()
        assert again["cursor"] == 1
        assert Message.objects.filter(conversation=_conversation(key)).count() == 1
        conversation = _conversation(key)
        assert conversation.is_unread is False

    def test_open_without_opened_workflow_stores_nothing(self, chatbot):
        client, key = _start(chatbot)
        data = _post(client, "/api/chat/open/", {"session_key": key}).json()
        assert data == {
            "messages": [],
            "cursor": 0,
            "expect": None,
            "handoff": None,
            "ended": False,
        }
        session = ChatSession.objects.get(session_key=key)
        assert session.conversation_id is None and session.contact_id is None
        assert session.opened_at is not None


# ── Who answers when the bot shouldn't ─────────────────────────────────────────


class TestHandover:
    def test_paused_conversation_gets_no_bot_reply(self, chatbot, provider):
        client, key = _start(chatbot)
        _say(client, key, "first")
        _conversation(key).pause_automation(2)
        data = _say(client, key, "are you there?").json()
        assert data["messages"] == []
        assert provider.calls == 1
        last = Message.objects.filter(conversation=_conversation(key)).last()
        assert last.metadata["handled_by"] == "paused"

    def test_ai_off_sends_holding_reply_once(self, chatbot, monkeypatch):
        monkeypatch.setattr("apps.ai.api.is_available", lambda account: False)
        client, key = _start(chatbot)
        first = _say(client, key, "hello").json()
        second = _say(client, key, "anyone?").json()
        assert [m["from"] for m in first["messages"]] == ["bot"]
        assert "someone from the team" in first["reply"].lower()
        assert first["handoff_suggested"] is True
        assert second["messages"] == []
        holding = Message.objects.get(
            conversation=_conversation(key), direction="outbound"
        )
        assert holding.metadata.get("auto_kind") == "holding"

    def test_bot_replies_are_marked_as_ai_not_a_person(self, chatbot, provider):
        client, key = _start(chatbot)
        _say(client, key, "hi")
        reply = Message.objects.get(
            conversation=_conversation(key), direction="outbound"
        )
        assert reply.metadata["sent_by"] == "ai"


# ── The team's replies reach the widget ────────────────────────────────────────


@pytest.fixture
def teammate_client(account):
    user = get_user_model().objects.create_user(
        "jo", "jo@example.com", "pw", first_name="Jo"
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return client


class TestTeamReplies:
    def test_inbox_reply_reaches_the_widget(self, chatbot, provider, teammate_client):
        client, key = _start(chatbot)
        cursor = _say(client, key, "can I speak to someone?").json()["cursor"]
        conversation = _conversation(key)

        resp = teammate_client.post(
            f"/inbox/{conversation.public_id}/",
            {"action": "reply", "body": "Hi, Jo here!", "client_key": "abc123def456"},
        )
        assert resp.status_code in (200, 302)

        polled = _poll(client, key, cursor).json()
        assert polled["messages"] == [{"from": "team", "text": "Hi, Jo here!"}]
        assert polled["cursor"] == cursor + 1
        assert _poll(client, key, polled["cursor"]).json()["messages"] == []

        sent = Message.objects.filter(conversation=conversation).last()
        assert "sent_by" not in sent.metadata
        assert sent.metadata["sent_by_user"]

    def test_repeated_inbox_send_is_shown_once(self, chatbot, provider, account):
        from apps.core.actions import run_action

        client, key = _start(chatbot)
        _say(client, key, "hi")
        conversation = _conversation(key)
        for _ in range(2):
            run_action(
                "reply",
                {"account": account},
                conversation=conversation,
                body="Only once",
                idempotency_key="inbox:1:2:samekey",
            )
        assert (
            Message.objects.filter(conversation=conversation, body="Only once").count()
            == 1
        )

    def test_teammate_reply_pauses_the_bot_when_business_asks(
        self, chatbot, account, provider, teammate_client
    ):
        account.pause_automation_hours = 4
        account.save(update_fields=["pause_automation_hours"])
        client, key = _start(chatbot)
        _say(client, key, "hi")
        conversation = _conversation(key)
        teammate_client.post(
            f"/inbox/{conversation.public_id}/",
            {"action": "reply", "body": "I'll take it from here"},
        )
        conversation.refresh_from_db()
        assert conversation.automation_paused
        assert _say(client, key, "thanks").json()["messages"] == []
        assert provider.calls == 1

    def test_inbox_lists_website_chat(self, chatbot, provider, teammate_client):
        client, key = _start(chatbot)
        _say(client, key, "hello from the website")
        resp = teammate_client.get("/inbox/")
        assert resp.status_code == 200
        assert 'data-channel="website_chat"' in resp.content.decode()


# ── Poll endpoint ──────────────────────────────────────────────────────────────


class TestPoll:
    def test_poll_before_any_message(self, chatbot):
        client, key = _start(chatbot)
        assert _poll(client, key, 0).json() == {
            "messages": [],
            "cursor": 0,
            "expect": None,
            "handoff": None,
            "ended": False,
        }

    def test_poll_has_its_own_rate_limit(self, chatbot, provider, monkeypatch):
        monkeypatch.setattr("apps.chatbot.api.auth._RATE_LIMIT", 2)
        client, key = _start(chatbot)
        for _ in range(5):
            assert _poll(client, key, 0).status_code == 200
        assert _say(client, key, "still allowed").status_code == 200

    def test_poll_is_rate_limited(self, chatbot, monkeypatch):
        monkeypatch.setattr("apps.chatbot.api.auth._POLL_RATE_LIMIT", 2)
        client, key = _start(chatbot)
        statuses = [_poll(client, key, 0).status_code for _ in range(3)]
        assert statuses == [200, 200, 429]

    @pytest.mark.parametrize("cursor", [-1, "3", True, None])
    def test_bad_cursor_rejected(self, chatbot, cursor):
        client, key = _start(chatbot)
        assert _poll(client, key, cursor).status_code == 400

    def test_poll_from_unlisted_site_refused(self, chatbot):
        _, key = _start(chatbot)
        resp = _poll(Client(HTTP_ORIGIN="https://evil.example"), key, 0)
        assert resp.status_code == 403
        assert "Access-Control-Allow-Origin" not in resp


# ── Configuration rules ────────────────────────────────────────────────────────


class TestConfiguration:
    def test_reply_steps_valid_on_website_triggers(self):
        for trigger in ("website_chat.message_received", "website_chat.opened"):
            assert (
                validate_definition(
                    {"trigger": {"type": trigger}, "steps": REPLY_STEPS}
                )
                == []
            )

    def test_keyword_match_allowed_on_website_message_trigger(self):
        definition = {
            "trigger": {
                "type": "website_chat.message_received",
                "match": {"any": ["price"]},
            },
            "steps": REPLY_STEPS,
        }
        assert validate_definition(definition) == []

    def test_bot_workflow_must_start_from_website_chat(self, chatbot, account):
        chatbot.workflow = _workflow(
            account, {"type": "conversation.message_received"}, REPLY_STEPS
        )
        with pytest.raises(ValidationError):
            chatbot.save()

    def test_bot_workflow_must_be_same_account(self, chatbot, another_account):
        chatbot.workflow = _workflow(
            another_account, {"type": "website_chat.message_received"}, REPLY_STEPS
        )
        with pytest.raises(ValidationError):
            chatbot.save()

    def test_website_triggers_hidden_until_released(self, settings):
        from apps.automation.builder import trigger_catalog

        settings.RELEASED_FEATURES = frozenset()
        hidden = {t["type"] for t in trigger_catalog()}
        assert "website_chat.message_received" not in hidden
        settings.RELEASED_FEATURES = frozenset({"chatbot"})
        shown = {t["type"] for t in trigger_catalog()}
        assert {"website_chat.message_received", "website_chat.opened"} <= shown


def test_conversation_channel_is_website_chat(chatbot, provider):
    client, key = _start(chatbot)
    _say(client, key, "hi")
    assert _conversation(key).channel == Conversation.Channel.WEBSITE_CHAT
    assert ChatbotConfig.objects.get(pk=chatbot.pk).workflow is None

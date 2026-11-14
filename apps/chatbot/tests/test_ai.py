"""Chatbot Studio P3 (docs/plans/chatbot-studio.md): AI in website chat.

The bot's built-in AI is the checked answer the inbox's automatic replies use
(``apps.ai.api.grounded_answer``), never takes actions, respects the site-wide switch for
automatic AI replies, and stays within the bot's daily AI limit. The same limit applies to AI
steps in the bot's workflow, which take their fallback way when it's reached.
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client

from apps.accounts.models import Membership
from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun
from apps.chatbot.models import ChatActionExecution, ChatSession
from apps.chatbot.services.ai_budget import take_ai_turn, used_today
from apps.conversations.models import Message

ORIGIN = "https://acme.com"
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


class FakeAI:
    def __init__(self):
        self.answers = 0
        self.classified = 0
        self.extracted = 0
        self.answer = {"ok": True, "text": "We ship to Leeds in 2 days.", "failed": ""}

    def grounded_answer(self, conversation, *, operation_id):
        self.answers += 1
        return dict(self.answer)

    def classify(self, account, text, labels, *, operation_id, corrections=()):
        self.classified += 1
        return {"label": "Price", "confidence": 0.95, "ai": True}

    def extract(self, account, text, fields, *, operation_id):
        self.extracted += 1
        return {"email": "jane@example.com"}


@pytest.fixture
def ai(monkeypatch):
    fake = FakeAI()
    monkeypatch.setattr("apps.ai.api.grounded_answer", fake.grounded_answer)
    monkeypatch.setattr("apps.ai.api.classify", fake.classify)
    monkeypatch.setattr("apps.ai.api.extract", fake.extract)
    monkeypatch.setattr("apps.ai.api.is_available", lambda account: True)
    return fake


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def _start(chatbot):
    client = Client(HTTP_ORIGIN=ORIGIN)
    key = _post(client, "/api/chat/init/", {"public_key": chatbot.public_key}).json()[
        "session_key"
    ]
    return client, key


def _say(client, key, text):
    return _post(client, "/api/chat/message/", {"session_key": key, "message": text})


def _outbound(key):
    session = ChatSession.objects.get(session_key=key)
    return list(
        Message.objects.filter(
            conversation=session.conversation, direction="outbound"
        ).values_list("body", "metadata")
    )


# ── The bot's built-in AI ───────────────────────────────────────────────────────


class TestBuiltInAI:
    def test_answers_with_the_checked_answer(self, chatbot, ai):
        client, key = _start(chatbot)
        data = _say(client, key, "Do you ship to Leeds?").json()
        assert data["messages"] == [
            {"from": "bot", "text": "We ship to Leeds in 2 days."}
        ]
        assert data["handoff_suggested"] is False
        [(body, meta)] = _outbound(key)
        assert meta["sent_by"] == "ai"

    def test_declined_answer_goes_to_the_team_once(self, chatbot, ai):
        ai.answer = {
            "ok": False,
            "text": "",
            "failed": "It mentions a price not in your notes.",
        }
        client, key = _start(chatbot)
        first = _say(client, key, "How much is the X200?").json()
        second = _say(client, key, "And the X300?").json()
        assert first["handoff_suggested"] is True
        assert "someone from the team" in first["reply"].lower()
        assert second["messages"] == []
        assert len(_outbound(key)) == 1

    def test_asking_for_a_person_skips_ai(self, chatbot, ai):
        client, key = _start(chatbot)
        data = _say(client, key, "Can I talk to a real person please").json()
        assert ai.answers == 0
        assert data["handoff_suggested"] is True

    @pytest.mark.parametrize(
        "text",
        ["what agency do you use?", "humanitarian shipping?", "I need an agenda"],
    )
    def test_ordinary_words_are_not_a_request_for_a_person(self, chatbot, ai, text):
        client, key = _start(chatbot)
        _say(client, key, text)
        assert ai.answers == 1

    def test_site_wide_switch_stops_automatic_ai(self, chatbot, ai, settings):
        settings.AI_AUTONOMY_ENABLED = False
        client, key = _start(chatbot)
        data = _say(client, key, "hello").json()
        assert ai.answers == 0
        assert data["handoff_suggested"] is True

    def test_never_takes_actions(self, chatbot, ai, action):
        ai.answer = {
            "ok": True,
            "text": "Sure [ACTION:check_payment_status]",
            "failed": "",
        }
        client, key = _start(chatbot)
        _say(client, key, "where's my payment?")
        assert not ChatActionExecution.objects.exists()


# ── The bot's daily AI limit ────────────────────────────────────────────────────


class TestDailyLimit:
    def test_take_ai_turn_counts_to_the_limit(self, chatbot):
        chatbot.ai_daily_limit = 2
        chatbot.save()
        assert [take_ai_turn(chatbot.pk) for _ in range(3)] == [True, True, False]
        assert used_today(chatbot.pk) == 3
        assert take_ai_turn(None) is True  # not a chatbot run

    def test_zero_means_no_ai(self, chatbot, ai):
        chatbot.ai_daily_limit = 0
        chatbot.save()
        client, key = _start(chatbot)
        data = _say(client, key, "hi").json()
        assert ai.answers == 0 and data["handoff_suggested"] is True

    def test_limit_reached_hands_over(self, chatbot, ai):
        chatbot.ai_daily_limit = 1
        chatbot.save()
        client, key = _start(chatbot)
        _say(client, key, "first")
        data = _say(client, key, "second").json()
        assert ai.answers == 1
        assert data["handoff_suggested"] is True


# ── AI steps in the bot's workflow ──────────────────────────────────────────────


def _bot_flow(chatbot, account, steps):
    chatbot.workflow = Workflow.objects.create(
        account=account,
        name="AI flow",
        slug="ai-flow",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "website_chat.message_received"},
            "steps": steps,
        },
    )
    chatbot.save()


ANSWER_STEPS = [
    {"id": "a", "type": "ai_answer", "on_handoff": "hold", "next": "stop"},
    {
        "id": "hold",
        "type": "reply_text",
        "text": "A teammate will reply soon.",
        "next": "stop",
    },
    {"id": "stop", "type": "stop"},
]

CLASSIFY_STEPS = [
    {
        "id": "sort",
        "type": "ai_classify",
        "labels": [
            {"label": "Price", "keywords": ["how much"]},
            {"label": "Delivery", "keywords": ["deliver"]},
        ],
        "routes": {"Price": "price", "Delivery": "delivery"},
        "on_unsure": "unsure",
    },
    {
        "id": "price",
        "type": "reply_text",
        "text": "Prices are on our site.",
        "next": "stop",
    },
    {
        "id": "delivery",
        "type": "reply_text",
        "text": "We deliver in 2 days.",
        "next": "stop",
    },
    {
        "id": "unsure",
        "type": "reply_text",
        "text": "Let me get a teammate.",
        "next": "stop",
    },
    {"id": "stop", "type": "stop"},
]


class TestWorkflowAISteps:
    def test_ai_answer_step_answers_in_website_chat(self, chatbot, account, ai):
        _bot_flow(chatbot, account, ANSWER_STEPS)
        client, key = _start(chatbot)
        data = _say(client, key, "Do you ship to Leeds?").json()
        assert data["messages"] == [
            {"from": "bot", "text": "We ship to Leeds in 2 days."}
        ]
        [(_, meta)] = _outbound(key)
        assert meta["sent_by"] == "automation"
        assert WorkflowRun.objects.get().context["chatbot_id"] == chatbot.pk

    def test_ai_answer_step_over_the_limit_takes_its_fallback(
        self, chatbot, account, ai
    ):
        chatbot.ai_daily_limit = 0
        chatbot.save()
        _bot_flow(chatbot, account, ANSWER_STEPS)
        client, key = _start(chatbot)
        data = _say(client, key, "Do you ship to Leeds?").json()
        assert data["messages"] == [
            {"from": "bot", "text": "A teammate will reply soon."}
        ]
        assert ai.answers == 0
        step = WorkflowStepRun.objects.get(step_id="a")
        assert step.result["why"] == "This chat's daily AI limit has been reached."

    def test_keyword_sorting_needs_no_ai_even_over_the_limit(
        self, chatbot, account, ai
    ):
        chatbot.ai_daily_limit = 0
        chatbot.save()
        _bot_flow(chatbot, account, CLASSIFY_STEPS)
        client, key = _start(chatbot)
        data = _say(client, key, "do you deliver to Leeds?").json()
        assert data["messages"] == [{"from": "bot", "text": "We deliver in 2 days."}]
        assert ai.classified == 0

    def test_ai_sorting_within_the_limit(self, chatbot, account, ai):
        _bot_flow(chatbot, account, CLASSIFY_STEPS)
        client, key = _start(chatbot)
        data = _say(client, key, "what would the X200 cost me").json()
        assert data["messages"] == [{"from": "bot", "text": "Prices are on our site."}]
        assert ai.classified == 1 and used_today(chatbot.pk) == 1

    def test_ai_sorting_over_the_limit_goes_the_unsure_way(self, chatbot, account, ai):
        chatbot.ai_daily_limit = 0
        chatbot.save()
        _bot_flow(chatbot, account, CLASSIFY_STEPS)
        client, key = _start(chatbot)
        data = _say(client, key, "what would the X200 cost me").json()
        assert data["messages"] == [{"from": "bot", "text": "Let me get a teammate."}]
        assert ai.classified == 0
        assert (
            WorkflowStepRun.objects.get(step_id="sort").result["method"] == "ai_limit"
        )

    def test_ai_extract_over_the_limit_goes_the_missing_way(self, chatbot, account, ai):
        chatbot.ai_daily_limit = 0
        chatbot.save()
        _bot_flow(
            chatbot,
            account,
            [
                {
                    "id": "x",
                    "type": "ai_extract",
                    "fields": [{"key": "email", "type": "email"}],
                    "on_missing": "ask",
                    "next": "ok",
                },
                {
                    "id": "ask",
                    "type": "reply_text",
                    "text": "What's your email?",
                    "next": "stop",
                },
                {"id": "ok", "type": "reply_text", "text": "Got it.", "next": "stop"},
                {"id": "stop", "type": "stop"},
            ],
        )
        client, key = _start(chatbot)
        data = _say(client, key, "jane@example.com here").json()
        assert data["messages"] == [{"from": "bot", "text": "What's your email?"}]
        assert ai.extracted == 0

    def test_runs_outside_website_chat_have_no_bot_limit(self, account, ai):
        from apps.automation.ai_steps import _within_budget

        run = WorkflowRun(context={"conversation_id": "con_x"})
        assert _within_budget(run) is True


# ── The bot's page ──────────────────────────────────────────────────────────────


@pytest.fixture
def owner(account, settings):
    settings.RELEASED_FEATURES = frozenset({"chatbot"})
    user = get_user_model().objects.create_user("o", "o@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return client


class TestOwnerPage:
    def test_page_says_how_it_answers(self, owner, chatbot, monkeypatch):
        monkeypatch.setattr("apps.ai.api.unavailable_reason", lambda account: None)
        take_ai_turn(chatbot.pk)
        page = owner.get(f"/chatbot/{chatbot.pk}/").content.decode()
        assert "How it answers" in page
        assert "1 of 200 used today" in page
        assert "Knowledge sources" not in page and "Add action" not in page

    def test_page_says_when_ai_is_off(self, owner, chatbot, monkeypatch):
        monkeypatch.setattr(
            "apps.ai.api.unavailable_reason",
            lambda account: "The AI assistant isn't included in this business's plan.",
        )
        page = owner.get(f"/chatbot/{chatbot.pk}/").content.decode()
        assert "isn&#x27;t included in this business&#x27;s plan" in page

    def _edit(self, owner, chatbot, limit):
        return owner.post(
            f"/chatbot/{chatbot.pk}/edit/",
            {
                "name": chatbot.name,
                "purpose": chatbot.purpose,
                "position": chatbot.position,
                "primary_color": chatbot.primary_color,
                "allowed_domains": "https://acme.com",
                "is_active": "on",
                "ai_daily_limit": limit,
            },
        )

    def test_owner_sets_the_daily_limit(self, owner, chatbot):
        assert self._edit(owner, chatbot, "50").status_code == 302
        chatbot.refresh_from_db()
        assert chatbot.ai_daily_limit == 50

    @pytest.mark.parametrize("bad", ["-1", "abc", "20000", "2.5"])
    def test_bad_limit_is_refused(self, owner, chatbot, bad):
        resp = self._edit(owner, chatbot, bad)
        assert resp.status_code == 200
        assert "AI answers per day must be a whole number" in resp.content.decode()
        chatbot.refresh_from_db()
        assert chatbot.ai_daily_limit == 200

    def test_retired_pages_are_gone(self, owner, chatbot):
        assert owner.post(f"/chatbot/{chatbot.pk}/knowledge/").status_code == 404
        assert owner.get(f"/chatbot/{chatbot.pk}/actions/new/").status_code == 404

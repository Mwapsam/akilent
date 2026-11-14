"""Chatbot Studio P2 (docs/plans/chatbot-studio.md): buttons, lists and questions in website chat.

The bot's automation asks with buttons or a list, or asks a question; the widget gets what the
bot is waiting for as ``expect`` and sends a tapped option back as ``choice``. Routing reuses the
interaction model Instagram quick replies use (``claim_reply``), so WhatsApp-style routes work.
"""

import json
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.test import Client

from apps.automation.models import Workflow, WorkflowInteraction, WorkflowRun
from apps.automation.workflow_engine import validate_definition
from apps.chatbot.models import ChatSession
from apps.contacts.models import CustomAttributeDef
from apps.conversations.models import Message

ORIGIN = "https://acme.com"
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def provider(monkeypatch):
    fake = SimpleNamespace(calls=0)

    def grounded_answer(conversation, *, operation_id):
        fake.calls += 1
        return {"ok": True, "text": "AI answer", "failed": ""}

    monkeypatch.setattr("apps.ai.api.grounded_answer", grounded_answer)
    monkeypatch.setattr("apps.ai.api.is_available", lambda account: True)
    return fake


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def _bot_with(chatbot, account, steps, trigger="website_chat.message_received"):
    wf = Workflow.objects.create(
        account=account,
        name="Website flow",
        slug="website-flow",
        status=Workflow.Status.PUBLISHED,
        definition={"trigger": {"type": trigger}, "steps": steps},
    )
    chatbot.workflow = wf
    chatbot.save()
    return wf


def _start(chatbot):
    client = Client(HTTP_ORIGIN=ORIGIN)
    key = _post(client, "/api/chat/init/", {"public_key": chatbot.public_key}).json()[
        "session_key"
    ]
    return client, key


def _say(client, key, text, choice=None):
    payload = {"session_key": key, "message": text}
    if choice is not None:
        payload["choice"] = choice
    return _post(client, "/api/chat/message/", payload)


SALES_OR_SUPPORT = [
    {
        "id": "ask",
        "type": "send_buttons",
        "text": "Sales or support?",
        "buttons": [{"title": "Sales"}, {"title": "Support"}],
        "next": "wait",
    },
    {
        "id": "wait",
        "type": "wait_for_reply",
        "timeout_seconds": 3600,
        "routes": {"sales": "sales", "support": "support"},
    },
    {
        "id": "sales",
        "type": "reply_text",
        "text": "Our sales team will help.",
        "next": "stop",
    },
    {
        "id": "support",
        "type": "reply_text",
        "text": "Support is on it.",
        "next": "stop",
    },
    {"id": "stop", "type": "stop"},
]


class TestButtons:
    def test_question_comes_with_tappable_choices(self, chatbot, account, provider):
        _bot_with(chatbot, account, SALES_OR_SUPPORT)
        client, key = _start(chatbot)
        data = _say(client, key, "hi").json()

        assert data["messages"] == [{"from": "bot", "text": "Sales or support?"}]
        expect = data["expect"]
        assert expect["kind"] == "choice" and expect["style"] == "buttons"
        assert [o["title"] for o in expect["options"]] == ["Sales", "Support"]
        assert all(
            o["choice"].endswith((":sales", ":support")) for o in expect["options"]
        )
        assert provider.calls == 0

    def test_tapping_a_choice_takes_its_route(self, chatbot, account, provider):
        _bot_with(chatbot, account, SALES_OR_SUPPORT)
        client, key = _start(chatbot)
        sales = _say(client, key, "hi").json()["expect"]["options"][0]
        data = _say(client, key, sales["title"], choice=sales["choice"]).json()

        assert data["messages"] == [
            {"from": "bot", "text": "Our sales team will help."}
        ]
        assert data["expect"] is None
        session = ChatSession.objects.get(session_key=key)
        tap = Message.objects.filter(
            conversation=session.conversation, direction="inbound"
        ).last()
        assert tap.body == "Sales"
        assert tap.metadata["handled_by"] == "automation_reply"
        assert WorkflowRun.objects.get().status == WorkflowRun.Status.COMPLETED

    def test_typing_the_title_works_too(self, chatbot, account, provider):
        _bot_with(chatbot, account, SALES_OR_SUPPORT)
        client, key = _start(chatbot)
        _say(client, key, "hi")
        data = _say(client, key, "support").json()
        assert data["messages"] == [{"from": "bot", "text": "Support is on it."}]

    def test_a_made_up_choice_is_just_typed_text(self, chatbot, account, provider):
        _bot_with(chatbot, account, SALES_OR_SUPPORT)
        client, key = _start(chatbot)
        _say(client, key, "hi")
        data = _say(client, key, "what are your hours?", choice="forged:sales").json()
        assert data["messages"] == [{"from": "bot", "text": "AI answer"}]
        assert WorkflowRun.objects.get().status == WorkflowRun.Status.WAITING
        assert data["expect"]["kind"] == "choice"  # still waiting on the question

    def test_poll_and_resume_offer_the_open_question(self, chatbot, account, provider):
        _bot_with(chatbot, account, SALES_OR_SUPPORT)
        client, key = _start(chatbot)
        cursor = _say(client, key, "hi").json()["cursor"]
        polled = _post(
            client, "/api/chat/poll/", {"session_key": key, "cursor": cursor}
        ).json()
        assert polled["expect"]["kind"] == "choice"
        resumed = _post(
            client,
            "/api/chat/init/",
            {"public_key": chatbot.public_key, "session_key": key},
        ).json()
        assert resumed["expect"]["kind"] == "choice"

    def test_greeting_with_buttons_on_open(self, chatbot, account, provider):
        _bot_with(chatbot, account, SALES_OR_SUPPORT, trigger="website_chat.opened")
        client, key = _start(chatbot)
        data = _post(client, "/api/chat/open/", {"session_key": key}).json()
        assert data["messages"] == [{"from": "bot", "text": "Sales or support?"}]
        option = data["expect"]["options"][1]
        reply = _say(client, key, option["title"], choice=option["choice"]).json()
        assert reply["messages"] == [{"from": "bot", "text": "Support is on it."}]

    @pytest.mark.parametrize("bad", [123, ["x"], "x" * 301])
    def test_bad_choice_rejected(self, chatbot, bad):
        client, key = _start(chatbot)
        assert _say(client, key, "hi", choice=bad).status_code == 400


class TestList:
    STEPS = [
        {
            "id": "ask",
            "type": "send_list",
            "text": "Which product?",
            "button": "See products",
            "rows": [
                {"id": "x200", "title": "X200 laptop", "description": "14 inch, 16GB"},
                {"id": "x300", "title": "X300 laptop", "description": "16 inch, 32GB"},
            ],
            "next": "wait",
        },
        {
            "id": "wait",
            "type": "wait_for_reply",
            "timeout_seconds": 3600,
            "routes": {"x200": "a", "x300": "b"},
        },
        {"id": "a", "type": "reply_text", "text": "Great pick: X200.", "next": "stop"},
        {"id": "b", "type": "reply_text", "text": "Great pick: X300.", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ]

    def test_list_offered_and_routed(self, chatbot, account, provider):
        _bot_with(chatbot, account, self.STEPS)
        client, key = _start(chatbot)
        expect = _say(client, key, "hi").json()["expect"]
        assert expect["style"] == "list" and expect["button"] == "See products"
        assert [o["description"] for o in expect["options"]] == [
            "14 inch, 16GB",
            "16 inch, 32GB",
        ]
        pick = expect["options"][1]
        data = _say(client, key, pick["title"], choice=pick["choice"]).json()
        assert data["messages"] == [{"from": "bot", "text": "Great pick: X300."}]

    def test_list_allowed_for_website_even_with_instagram(self, account):
        from apps.instagram.models.account import InstagramBusinessAccount

        InstagramBusinessAccount.objects.create(
            account=account,
            instagram_business_account_id="IBA-P2",
            page_id="PAGE-P2",
            access_token="t",
            verify_token="v",
            is_active=True,
        )
        website = {
            "trigger": {"type": "website_chat.message_received"},
            "steps": self.STEPS,
        }
        assert validate_definition(website, account=account) == []
        anywhere = {
            "trigger": {"type": "conversation.message_received"},
            "steps": self.STEPS,
        }
        assert validate_definition(anywhere, account=account) != []


def _question(attribute, then="Thanks!"):
    return [
        {
            "id": "q",
            "type": "ask_question",
            "question": "One question first",
            "attribute": attribute,
            "next": "done",
        },
        {"id": "done", "type": "reply_text", "text": then, "next": "stop"},
        {"id": "stop", "type": "stop"},
    ]


class TestQuestions:
    def _attr(self, account, key, type_, options=None):
        return CustomAttributeDef.objects.create(
            account=account,
            entity="contact",
            key=key,
            label=key.title(),
            type=type_,
            options=options or [],
        )

    def test_number_question_gets_a_number_input_and_saves(
        self, chatbot, account, provider
    ):
        self._attr(account, "budget", "number")
        _bot_with(chatbot, account, _question("budget"))
        client, key = _start(chatbot)
        data = _say(client, key, "hi").json()
        assert data["expect"] == {"kind": "text", "input": "number"}
        done = _say(client, key, "800").json()
        assert done["messages"] == [{"from": "bot", "text": "Thanks!"}]
        contact = ChatSession.objects.get(session_key=key).contact
        contact.refresh_from_db()
        assert contact.attributes["budget"] == "800.00"

    def test_yes_no_question_offers_taps_and_saves_the_value(
        self, chatbot, account, provider
    ):
        self._attr(account, "wants_delivery", "boolean")
        _bot_with(chatbot, account, _question("wants_delivery"))
        client, key = _start(chatbot)
        expect = _say(client, key, "hi").json()["expect"]
        assert [o["title"] for o in expect["options"]] == ["Yes", "No"]
        yes = expect["options"][0]
        _say(client, key, yes["title"], choice=yes["choice"])
        contact = ChatSession.objects.get(session_key=key).contact
        contact.refresh_from_db()
        assert contact.attributes["wants_delivery"] is True

    def test_choice_question_saves_the_key_shows_the_label(
        self, chatbot, account, provider
    ):
        self._attr(
            account,
            "use",
            "choice",
            options=[
                {"key": "work", "label": "For work"},
                {"key": "gaming", "label": "Gaming"},
            ],
        )
        _bot_with(chatbot, account, _question("use"))
        client, key = _start(chatbot)
        option = _say(client, key, "hi").json()["expect"]["options"][1]
        assert option == {"title": "Gaming", "choice": "gaming"}
        _say(client, key, option["title"], choice=option["choice"])
        session = ChatSession.objects.get(session_key=key)
        session.contact.refresh_from_db()
        assert session.contact.attributes["use"] == "gaming"
        assert Message.objects.filter(
            conversation=session.conversation, direction="inbound", body="Gaming"
        ).exists()

    def test_email_like_field_gets_an_email_keyboard(self, chatbot, account, provider):
        self._attr(account, "work_email", "string")
        _bot_with(chatbot, account, _question("work_email"))
        client, key = _start(chatbot)
        assert _say(client, key, "hi").json()["expect"] == {
            "kind": "text",
            "input": "email",
        }

    def test_answered_question_is_no_longer_offered(self, chatbot, account, provider):
        self._attr(account, "budget", "number")
        _bot_with(chatbot, account, _question("budget"))
        client, key = _start(chatbot)
        _say(client, key, "hi")
        assert _say(client, key, "500").json()["expect"] is None
        assert not WorkflowInteraction.objects.filter(
            status=WorkflowInteraction.Status.OPEN
        ).exists()


class TestPalette:
    def test_conversation_steps_usable_with_a_live_bot(
        self, settings, chatbot, account
    ):
        from apps.automation.builder import step_catalog

        settings.WHATSAPP_ENABLED = False
        settings.INSTAGRAM_ENABLED = False
        settings.FACEBOOK_ENABLED = False
        settings.RELEASED_FEATURES = frozenset()
        hidden = {s["type"]: s for s in step_catalog(account)}
        assert hidden["send_buttons"]["available"] is False

        settings.RELEASED_FEATURES = frozenset({"chatbot"})
        shown = {s["type"]: s for s in step_catalog(account)}
        for step in (
            "reply_text",
            "send_buttons",
            "send_list",
            "ask_question",
            "wait_for_reply",
        ):
            assert shown[step]["available"], step

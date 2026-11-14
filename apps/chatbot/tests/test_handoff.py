"""Chatbot Studio P5 (docs/plans/chatbot-studio.md): hand-off to a person, end state, outcomes.

A visitor can ask for a person (the widget's button, or by saying so). The bot steps back, the
team gets an inbox note saying why and one email, and the widget shows "a person will reply
here" until a teammate does. A closed conversation shows as ended. Analytics counts the funnel
from the real leads and orders.
"""

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Membership
from apps.automation.models import Workflow
from apps.chatbot import analytics
from apps.chatbot.models import ChatSession
from apps.chatbot.services.handoff import HANDOFF_REPLY
from apps.chatbot.services.inbound import HOLDING_REPLY
from apps.conversations.models import Conversation, Message

ORIGIN = "https://acme.com"
pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def ai(monkeypatch):
    fake = SimpleNamespace(calls=0, ok=True)

    def grounded_answer(conversation, *, operation_id):
        fake.calls += 1
        if fake.ok:
            return {"ok": True, "text": "We open at 9.", "failed": ""}
        return {"ok": False, "text": "", "failed": "not in Knowledge"}

    monkeypatch.setattr("apps.ai.api.grounded_answer", grounded_answer)
    monkeypatch.setattr("apps.ai.api.is_available", lambda account: True)
    return fake


@pytest.fixture
def owner_user(account):
    user = get_user_model().objects.create_user("own", "owner@acme.test", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


@pytest.fixture
def now_commit(monkeypatch):
    """Run the hand-off email now (tests don't commit, so on_commit callbacks never fire).

    Only ours: other on_commit work (AI suggestions for the inbox) isn't part of this."""
    from apps.chatbot import tasks

    def run_now(callback, *args, **kwargs):
        name = getattr(callback, "__qualname__", "")
        if "hand_to_team" in name:
            callback()

    monkeypatch.setattr("django.db.transaction.on_commit", run_now)
    return tasks


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """System emails sent (they go through Akilent's own mail provider, not Django's)."""
    outbox = []
    monkeypatch.setattr(
        "apps.email.services.send.send_system_email",
        lambda **kwargs: outbox.append(SimpleNamespace(**kwargs)),
    )
    return outbox


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def _start(chatbot, origin=ORIGIN):
    client = Client(HTTP_ORIGIN=origin)
    key = _post(client, "/api/chat/init/", {"public_key": chatbot.public_key}).json()[
        "session_key"
    ]
    return client, key


def _say(client, key, text, choice=None):
    payload = {"session_key": key, "message": text}
    if choice is not None:
        payload["choice"] = choice
    return _post(client, "/api/chat/message/", payload).json()


def _handoff(client, key):
    return _post(client, "/api/chat/handoff/", {"session_key": key})


def _poll(client, key, cursor):
    return _post(
        client, "/api/chat/poll/", {"session_key": key, "cursor": cursor}
    ).json()


def _session(key):
    return ChatSession.objects.select_related("conversation").get(session_key=key)


def _team_reply(conversation, text="Hi, Sam here."):
    return Message.objects.create(
        account=conversation.account,
        conversation=conversation,
        direction=Message.Direction.OUTBOUND,
        body=text,
        timestamp=timezone.now(),
        metadata={},
    )


# ── The button ──────────────────────────────────────────────────────────────────


class TestTalkToAPerson:
    def test_button_hands_over(self, sent, chatbot, ai, owner_user, now_commit):
        client, key = _start(chatbot)
        _say(client, key, "Hello")
        resp = _handoff(client, key)
        assert resp.status_code == 200
        data = resp.json()
        assert data["messages"] == [{"from": "bot", "text": HANDOFF_REPLY}]
        assert data["handoff"] == {"asks_details": True}

        session = _session(key)
        conversation = session.conversation
        assert session.handoff_reason == "asked" and session.handoff_at
        assert conversation.automation_paused
        assert conversation.is_unread
        note = Message.objects.get(
            conversation=conversation, direction=Message.Direction.SYSTEM
        )
        assert note.body == (
            "Website chat handed to your team: The visitor asked for a person."
        )
        assert len(sent) == 1
        assert sent[0].to_email == "owner@acme.test"
        assert "needs a person" in sent[0].subject

    def test_works_before_the_visitor_has_written(
        self, chatbot, owner_user, now_commit
    ):
        client, key = _start(chatbot)
        data = _handoff(client, key).json()
        assert data["messages"] == [{"from": "bot", "text": HANDOFF_REPLY}]
        assert _session(key).conversation is not None

    def test_second_tap_changes_nothing(
        self, sent, chatbot, ai, owner_user, now_commit
    ):
        client, key = _start(chatbot)
        _handoff(client, key)
        again = _handoff(client, key).json()
        assert again["messages"] == []
        assert len(sent) == 1
        assert Message.objects.filter(direction=Message.Direction.SYSTEM).count() == 1

    def test_bot_stays_quiet_after(self, chatbot, ai, owner_user, now_commit):
        client, key = _start(chatbot)
        _handoff(client, key)
        data = _say(client, key, "Are you there?")
        assert data["messages"] == [] and ai.calls == 0
        assert data["handoff"] == {"asks_details": True}

    def test_bots_open_question_is_withdrawn(self, chatbot, account, ai, owner_user):
        wf = Workflow.objects.create(
            account=account,
            name="Q",
            slug="q",
            status=Workflow.Status.PUBLISHED,
            definition={
                "trigger": {"type": "website_chat.opened"},
                "steps": [
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
                        "routes": {"sales": "stop", "support": "stop"},
                    },
                    {"id": "stop", "type": "stop"},
                ],
            },
        )
        chatbot.workflow = wf
        chatbot.save()
        client, key = _start(chatbot)
        opened = _post(client, "/api/chat/open/", {"session_key": key}).json()
        assert opened["expect"]["kind"] == "choice"
        assert _handoff(client, key).json()["expect"] is None

    def test_system_note_is_never_shown_to_the_visitor(self, chatbot, ai, owner_user):
        client, key = _start(chatbot)
        _say(client, key, "Hello")
        _handoff(client, key)
        resumed = _post(
            client,
            "/api/chat/init/",
            {"public_key": chatbot.public_key, "session_key": key},
        ).json()
        assert all("handed to your team" not in m["text"] for m in resumed["messages"])

    def test_leaving_details_stops_asking(self, chatbot, ai, owner_user):
        client, key = _start(chatbot)
        _handoff(client, key)
        _post(
            client,
            "/api/chat/identify/",
            {"session_key": key, "email": "jo@example.com", "name": "Jo"},
        )
        assert _poll(client, key, 1)["handoff"] == {"asks_details": False}

    def test_team_reply_clears_the_waiting_card(self, chatbot, ai, owner_user):
        client, key = _start(chatbot)
        _handoff(client, key)
        _team_reply(_session(key).conversation)
        data = _poll(client, key, 1)
        assert data["messages"] == [{"from": "team", "text": "Hi, Sam here."}]
        assert data["handoff"] is None

    def test_refused_from_another_site(self, chatbot, ai):
        client, key = _start(chatbot)
        other = Client(HTTP_ORIGIN="https://evil.test")
        assert _handoff(other, key).status_code == 403
        assert _session(key).handoff_at is None


# ── Hand-offs from what the visitor writes ──────────────────────────────────────


class TestAutomaticHandoff:
    def test_asking_in_words_is_the_same_as_the_button(
        self, chatbot, ai, owner_user, now_commit
    ):
        client, key = _start(chatbot)
        data = _say(client, key, "Can I talk to a human please")
        assert data["messages"][-1]["text"] == HANDOFF_REPLY
        session = _session(key)
        assert session.handoff_reason == "asked"
        assert session.conversation.automation_paused
        assert ai.calls == 0

    def test_ai_unsure_hands_over_but_keeps_answering(
        self, sent, chatbot, ai, owner_user, now_commit
    ):
        ai.ok = False
        client, key = _start(chatbot)
        data = _say(client, key, "Can I bring my dog?")
        assert data["messages"][-1]["text"] == HOLDING_REPLY
        assert data["handoff"] == {"asks_details": True}
        session = _session(key)
        assert session.handoff_reason == "ai_unsure"
        assert not session.conversation.automation_paused
        question = Message.objects.get(body="Can I bring my dog?")
        assert question.metadata["chatbot_unanswered"] == "ai_unsure"

        ai.ok = True  # the next question it can answer, it does
        assert _say(client, key, "When do you open?")["reply"] == "We open at 9."
        assert len(sent) == 1  # one email per chat

    def test_visitor_asking_after_the_bot_handed_over_is_noted(
        self, sent, chatbot, ai, owner_user, now_commit
    ):
        ai.ok = False
        client, key = _start(chatbot)
        _say(client, key, "Can I bring my dog?")
        _handoff(client, key)
        assert _session(key).handoff_reason == "asked"
        notes = Message.objects.filter(direction=Message.Direction.SYSTEM)
        assert notes.count() == 2
        assert len(sent) == 1


# ── Ended ───────────────────────────────────────────────────────────────────────


class TestEnded:
    def test_closed_conversation_shows_ended_until_they_write(self, chatbot, ai):
        client, key = _start(chatbot)
        _say(client, key, "Hello")
        conversation = _session(key).conversation
        Conversation.objects.filter(pk=conversation.pk).update(
            status=Conversation.Status.CLOSED
        )
        assert _poll(client, key, 2)["ended"] is True
        assert _say(client, key, "One more thing")["ended"] is False


# ── Outcomes ────────────────────────────────────────────────────────────────────


def _window():
    return analytics.period_range(30)


class TestFunnel:
    def test_counts_match_leads_and_orders(self, chatbot, account, ai):
        from apps.commerce.models import Order
        from apps.crm.models import Lead

        ChatSession.objects.create(chatbot=chatbot)  # loaded, never opened
        client, key = _start(chatbot)
        _post(client, "/api/chat/open/", {"session_key": key})
        _say(client, key, "Hi")
        _post(client, "/api/chat/identify/", {"session_key": key, "email": "a@b.co"})
        session = _session(key)
        Lead.objects.create(
            account=account,
            contact=session.contact,
            conversation=session.conversation,
            status=Lead.Status.QUALIFIED,
        )
        Order.objects.create(
            account=account,
            contact=session.contact,
            conversation=session.conversation,
            status=Order.Status.PAID,
            total=Decimal("120.00"),
            currency="USD",
        )
        counts = {s["key"]: s["count"] for s in analytics.funnel(chatbot, *_window())}
        assert counts == {
            "loaded": 2,
            "opened": 1,
            "wrote": 1,
            "details": 1,
            "lead": 1,
            "qualified": 1,
            "paid": 1,
        }
        assert analytics.revenue(chatbot, *_window()) == [
            {"currency": "USD", "total": Decimal("120.00"), "orders": 1}
        ]

    def test_other_bots_dont_count(self, chatbot, chatbot_no_domains, ai):
        ChatSession.objects.create(chatbot=chatbot_no_domains)
        assert analytics.funnel(chatbot, *_window())[0]["count"] == 0


class TestHandoffNumbers:
    def test_reasons_reply_time_and_gaps(self, chatbot, ai, owner_user):
        ai.ok = False
        client, key = _start(chatbot)
        _say(client, key, "Can I bring my dog?")
        client2, key2 = _start(chatbot)
        _handoff(client2, key2)

        session = _session(key)
        ChatSession.objects.filter(pk=session.pk).update(
            handoff_at=timezone.now() - timedelta(minutes=10)
        )
        _team_reply(session.conversation)

        numbers = analytics.handoffs(chatbot, *_window())
        assert numbers["total"] == 2 and numbers["replied"] == 1
        assert numbers["median_reply_minutes"] == 10
        assert {r["reason"] for r in numbers["reasons"]} == {"ai_unsure", "asked"}

        gaps = analytics.unanswered(chatbot, *_window())
        assert [g["text"] for g in gaps] == ["Can I bring my dog?"]

    def test_page_renders(self, chatbot, account, ai, owner_user, settings):
        settings.RELEASED_FEATURES = frozenset({"chatbot"})
        ai.ok = False
        client, key = _start(chatbot)
        _say(client, key, "Can I bring my dog?")
        owner = Client()
        owner.force_login(owner_user)
        page = owner.get(f"/chatbot/{chatbot.pk}/analytics/").content.decode()
        assert "From visit to sale" in page
        assert "Handed to your team" in page
        assert "Can I bring my dog?" in page


# ── Release gate journey (spec §11) ─────────────────────────────────────────────


def test_release_gate_journey(chatbot, account, ai, owner_user, sent, now_commit):
    """open → welcome → button → questions → lead → AI answer → hand-off → a teammate's
    reply from the inbox reaches the widget → the lead shows in the funnel."""
    from apps.automation import api as automation_api
    from apps.chatbot import starters
    from apps.core.actions import run_action

    wf = starters.install(chatbot, "capture-leads")
    automation_api.publish(wf)
    client, key = _start(chatbot)

    opened = _post(client, "/api/chat/open/", {"session_key": key}).json()
    assert opened["messages"][-1]["text"] == "Hi! 👋 How can we help today?"
    quote = next(o for o in opened["expect"]["options"] if o["title"] == "Get a quote")
    _say(client, key, "Get a quote", quote["choice"])
    _say(client, key, "Shelving for a shop")
    done = _say(client, key, "jo@example.com")
    assert "get back to you soon" in done["messages"][-1]["text"]

    assert _say(client, key, "When do you open?")["reply"] == "We open at 9."

    handed = _handoff(client, key).json()
    assert handed["messages"][-1]["text"] == HANDOFF_REPLY
    session = _session(key)
    run_action(
        "reply",
        {"account": account},
        conversation=session.conversation,
        body="Hi Jo, Sam here. Happy to help.",
        idempotency_key=f"inbox:{session.conversation.pk}:{owner_user.pk}:abcd1234",
    )
    later = _poll(client, key, handed["cursor"])
    assert later["messages"] == [
        {"from": "team", "text": "Hi Jo, Sam here. Happy to help."}
    ]
    assert later["handoff"] is None

    counts = {s["key"]: s["count"] for s in analytics.funnel(chatbot, *_window())}
    assert counts["opened"] == 1 and counts["wrote"] == 1 and counts["lead"] == 1
    assert (
        len(sent) == 2
    )  # the starter's "asked for a quote" email + the hand-off email

"""Chatbot Studio P4 (docs/plans/chatbot-studio.md): from "New chatbot" to a live bot.

An owner creates a bot from a starter conversation, tries it in the preview (nothing sent),
turns it on, pastes the code, and sees the Install check go green, without touching the
automation editor. Each starter works end to end through the real widget API.
"""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client

from apps.accounts.models import Membership
from apps.automation import api as automation_api
from apps.automation.models import Workflow
from apps.chatbot import starters
from apps.chatbot.models import ChatbotConfig
from apps.contacts.models import CustomAttributeDef
from apps.crm.models import Lead

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


def _user_client(account, role, settings):
    settings.RELEASED_FEATURES = frozenset({"chatbot"})
    email = f"{role}-{account.pk}@example.com"
    user = get_user_model().objects.create_user(email, email, "pw")
    Membership.objects.create(user=user, account=account, role=role)
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def owner(account, settings):
    return _user_client(account, Membership.Role.OWNER, settings)


def _create(owner, starter, website="acme.com", name="Site bot"):
    resp = owner.post(
        "/chatbot/new/", {"name": name, "starter": starter, "website": website}
    )
    assert resp.status_code == 302, resp.content.decode()[:500]
    return ChatbotConfig.objects.get(name=name)


def _post(client, path, payload):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def _widget(bot, origin=ORIGIN):
    client = Client(HTTP_ORIGIN=origin)
    resp = _post(client, "/api/chat/init/", {"public_key": bot.public_key})
    return client, resp


def _say(client, key, text, choice=None):
    payload = {"session_key": key, "message": text}
    if choice is not None:
        payload["choice"] = choice
    return _post(client, "/api/chat/message/", payload).json()


# ── Starters ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", list(starters.STARTERS))
def test_every_starter_can_be_turned_on_as_is(account, key):
    bot = ChatbotConfig.objects.create(account=account, name="B", allowed_domains=[])
    wf = starters.install(bot, key)
    assert automation_api.live_definition_problems(account, wf.definition) == []
    assert (
        wf.status == Workflow.Status.DRAFT
    )  # visitors see nothing until it's turned on
    bot.refresh_from_db()
    assert bot.workflow == wf


def test_starter_creates_the_fields_its_questions_save_into(account):
    bot = ChatbotConfig.objects.create(account=account, name="B", allowed_domains=[])
    starters.install(bot, "capture-leads")
    keys = set(
        CustomAttributeDef.objects.filter(account=account, entity="lead").values_list(
            "key", flat=True
        )
    )
    assert {"quote_request", "best_contact"} <= keys


# ── New chatbot ─────────────────────────────────────────────────────────────────


class TestCreate:
    def test_page_offers_the_starters(self, owner):
        page = owner.get("/chatbot/new/").content.decode()
        for s in starters.STARTERS.values():
            assert s["name"].replace("'", "&#x27;") in page

    def test_creates_bot_with_conversation_and_website(self, owner, account):
        bot = _create(owner, "answer-questions")
        assert bot.account == account
        assert bot.allowed_domains == ["https://acme.com"]
        assert bot.workflow is not None and bot.workflow.account == account
        assert bot.welcome_message == starters.STARTERS["answer-questions"]["welcome"]

    def test_website_is_optional(self, owner):
        assert _create(owner, "answer-questions", website="").allowed_domains == []

    def test_bad_website_keeps_what_was_typed(self, owner):
        resp = owner.post(
            "/chatbot/new/",
            {"name": "Mine", "starter": "capture-leads", "website": "not a site"},
        )
        page = resp.content.decode()
        assert resp.status_code == 200
        assert "website addresses" in page and 'value="Mine"' in page
        assert not ChatbotConfig.objects.filter(name="Mine").exists()
        assert not Workflow.objects.exists()  # nothing half-made

    def test_unknown_starter_is_refused(self, owner):
        resp = owner.post("/chatbot/new/", {"name": "Mine", "starter": "nope"})
        assert resp.status_code == 200
        assert not ChatbotConfig.objects.exists()


# ── Get it live ─────────────────────────────────────────────────────────────────


class TestGoLive:
    def test_checklist_until_live(self, owner, ai):
        bot = _create(owner, "capture-leads")
        page = owner.get(f"/chatbot/{bot.pk}/").content.decode()
        assert "Get it live" in page and "2 of 4 done" in page
        assert "Turn it on" in page

        assert owner.post(f"/chatbot/{bot.pk}/publish/").status_code == 302
        bot.workflow.refresh_from_db()
        assert bot.workflow.status == Workflow.Status.PUBLISHED
        assert "3 of 4 done" in owner.get(f"/chatbot/{bot.pk}/").content.decode()

        _widget(bot)  # the code is on the site and the chat loaded
        page = owner.get(f"/chatbot/{bot.pk}/").content.decode()
        assert "Get it live" not in page
        assert "Working · seen" in page

    def test_unsaved_edits_offer_put_changes_live(self, owner):
        bot = _create(owner, "answer-questions")
        owner.post(f"/chatbot/{bot.pk}/publish/")
        wf = bot.workflow
        wf.refresh_from_db()
        edited = json.loads(json.dumps(wf.definition))
        edited["steps"][1]["text"] = "A person will reply soon."
        automation_api.save_edits(wf, edited)
        assert "Put changes live" in owner.get(f"/chatbot/{bot.pk}/").content.decode()

    def test_only_owner_or_admin_turns_it_on(self, owner, account, settings):
        bot = _create(owner, "answer-questions")
        member = _user_client(account, Membership.Role.MEMBER, settings)
        page = member.get(f"/chatbot/{bot.pk}/").content.decode()
        assert "Only an owner or admin can turn it on" in page
        member.post(f"/chatbot/{bot.pk}/publish/")
        bot.workflow.refresh_from_db()
        assert bot.workflow.status == Workflow.Status.DRAFT

    def test_another_business_cannot_touch_it(self, owner, another_account, settings):
        bot = _create(owner, "answer-questions")
        rival = _user_client(another_account, Membership.Role.OWNER, settings)
        assert rival.get(f"/chatbot/{bot.pk}/").status_code == 404
        assert rival.post(f"/chatbot/{bot.pk}/publish/").status_code == 404
        assert _post(rival, f"/chatbot/{bot.pk}/preview/run/", {}).status_code == 404

    def test_a_broken_conversation_says_what_to_fix(self, owner):
        bot = _create(owner, "answer-questions")
        wf = bot.workflow
        wf.definition["steps"][0]["on_handoff"] = ""
        wf.save()
        resp = owner.post(f"/chatbot/{bot.pk}/publish/", follow=True)
        assert "Fix this first" in resp.content.decode()
        wf.refresh_from_db()
        assert wf.status == Workflow.Status.DRAFT


# ── Install check ───────────────────────────────────────────────────────────────


class TestInstallCheck:
    def test_not_seen_until_the_widget_loads(self, owner):
        bot = _create(owner, "answer-questions")
        assert "Not seen yet" in owner.get(f"/chatbot/{bot.pk}/").content.decode()

    def test_seen_is_saved_at_most_hourly(self, owner):
        bot = _create(owner, "answer-questions")
        _widget(bot)
        bot.refresh_from_db()
        first = bot.seen_on[ORIGIN]
        _widget(bot)
        bot.refresh_from_db()
        assert bot.seen_on[ORIGIN] == first

    def test_akilent_pages_dont_count(self, owner, settings):
        settings.SITE_URL = "https://app.akilent.test"
        bot = _create(owner, "answer-questions")
        _widget(bot, origin="https://app.akilent.test")
        bot.refresh_from_db()
        assert bot.seen_on == {}

    def test_a_refused_site_is_shown_and_can_be_added(self, owner):
        bot = _create(owner, "answer-questions")
        client, resp = _widget(bot, origin="https://www.acme.com")
        assert resp.status_code == 403
        page = owner.get(f"/chatbot/{bot.pk}/").content.decode()
        assert "https://www.acme.com" in page and "Add this website" in page

        owner.post(f"/chatbot/{bot.pk}/allow-site/", {"origin": "https://www.acme.com"})
        bot.refresh_from_db()
        assert bot.allowed_domains == ["https://acme.com", "https://www.acme.com"]
        assert _widget(bot, origin="https://www.acme.com")[1].status_code == 200

    def test_only_a_refused_site_can_be_added_that_way(self, owner):
        bot = _create(owner, "answer-questions")
        owner.post(f"/chatbot/{bot.pk}/allow-site/", {"origin": "https://evil.test"})
        bot.refresh_from_db()
        assert bot.allowed_domains == ["https://acme.com"]

    def test_refused_sites_are_capped(self, owner):
        bot = _create(owner, "answer-questions")
        for i in range(20):
            _widget(bot, origin=f"https://spam{i}.test")
        from apps.chatbot.services import install

        assert len(install.status(bot)["blocked"]) == 5


# ── Preview ─────────────────────────────────────────────────────────────────────


class TestPreview:
    def test_page_renders(self, owner):
        bot = _create(owner, "capture-leads")
        page = owner.get(f"/chatbot/{bot.pk}/preview/").content.decode()
        assert "Try Site bot" in page and "preview-cfg" in page

    def test_greeting_bot_plays_from_open(self, owner):
        bot = _create(owner, "capture-leads")
        url = f"/chatbot/{bot.pk}/preview/run/"
        out = _post(owner, url, {"message": "", "replies": []}).json()
        bots = [e for e in out["transcript"] if e["kind"] == "bot"]
        assert bots[0]["options"] == ["Get a quote", "Ask a question"]
        assert out["waiting"]["kind"] == "choice"

        out = _post(owner, url, {"message": "", "replies": ["Get a quote"]}).json()
        assert out["waiting"]["kind"] == "text"
        assert (
            out["transcript"][-1]["text"] == "Great! What would you like a quote for?"
        )

    def test_preview_uses_unpublished_changes_and_sends_nothing(self, owner):
        bot = _create(owner, "capture-leads")
        owner.post(f"/chatbot/{bot.pk}/publish/")
        wf = bot.workflow
        wf.refresh_from_db()
        edited = json.loads(json.dumps(wf.definition))
        edited["steps"][0]["text"] = "Hello there!"
        automation_api.save_edits(wf, edited)
        out = _post(owner, f"/chatbot/{bot.pk}/preview/run/", {}).json()
        assert any(e.get("text") == "Hello there!" for e in out["transcript"])
        from apps.conversations.models import Message

        assert not Message.objects.exists()


# ── Each starter, live through the widget ───────────────────────────────────────


def _live(owner, starter):
    bot = _create(owner, starter)
    owner.post(f"/chatbot/{bot.pk}/publish/")
    client, resp = _widget(bot)
    return bot, client, resp.json()["session_key"]


class TestStartersLive:
    def test_capture_leads(self, owner, ai):
        bot, client, key = _live(owner, "capture-leads")
        opened = _post(client, "/api/chat/open/", {"session_key": key}).json()
        assert opened["messages"][-1]["text"] == "Hi! 👋 How can we help today?"
        quote = next(
            o for o in opened["expect"]["options"] if o["title"] == "Get a quote"
        )

        data = _say(client, key, "Get a quote", quote["choice"])
        assert data["messages"][-1]["text"] == "Great! What would you like a quote for?"
        data = _say(client, key, "A new website")
        assert "best email or phone" in data["messages"][-1]["text"]
        data = _say(client, key, "jane@example.com")
        assert "get back to you soon" in data["messages"][-1]["text"]

        lead = Lead.objects.get(account=bot.account)
        assert lead.attributes["quote_request"] == "A new website"
        assert lead.attributes["best_contact"] == "jane@example.com"
        assert ai.calls == 0

    def test_answer_questions(self, owner, ai):
        bot, client, key = _live(owner, "answer-questions")
        assert _say(client, key, "When do you open?")["reply"] == "We open at 9."
        # Every question goes through it, not only the first.
        assert _say(client, key, "And Sundays?")["reply"] == "We open at 9."
        assert ai.calls == 2

    def test_answer_questions_hands_over(self, owner, ai):
        ai.ok = False
        bot, client, key = _live(owner, "answer-questions")
        data = _say(client, key, "Can I bring my dog?")
        assert "passed it to the team" in data["messages"][-1]["text"]

    def test_find_products(self, owner, ai, account):
        from apps.commerce.models import Product

        Product.objects.create(
            account=account,
            name="Red running shoes",
            slug="red-running-shoes",
            price=Decimal("45.00"),
            currency="USD",
            is_active=True,
        )
        bot, client, key = _live(owner, "find-products")
        data = _say(client, key, "red running shoes")
        assert data["messages"][-1]["text"].startswith("Red running shoes is USD 45.00")
        # Not a product: AI answers instead.
        assert _say(client, key, "When do you open?")["reply"] == "We open at 9."


# ── Settings, list, editor ──────────────────────────────────────────────────────


class TestSettings:
    def _edit(self, owner, bot, workflow):
        return owner.post(
            f"/chatbot/{bot.pk}/edit/",
            {
                "name": bot.name,
                "position": bot.position,
                "primary_color": bot.primary_color,
                "allowed_domains": "https://acme.com",
                "is_active": "on",
                "workflow": workflow,
            },
        )

    def test_switch_to_ai_only_and_back(self, owner):
        bot = _create(owner, "answer-questions")
        wf = bot.workflow
        assert self._edit(owner, bot, "").status_code == 302
        bot.refresh_from_db()
        assert bot.workflow is None
        assert self._edit(owner, bot, str(wf.pk)).status_code == 302
        bot.refresh_from_db()
        assert bot.workflow == wf

    def test_only_website_chat_automations_can_be_chosen(self, owner, account):
        bot = _create(owner, "answer-questions")
        other = Workflow.objects.create(
            account=account,
            name="WhatsApp flow",
            slug="wa",
            definition={"trigger": {"type": "conversation.message_received"}},
        )
        page = owner.get(f"/chatbot/{bot.pk}/edit/").content.decode()
        assert "WhatsApp flow" not in page
        resp = self._edit(owner, bot, str(other.pk))
        assert resp.status_code == 200
        bot.refresh_from_db()
        assert bot.workflow != other


def test_list_shows_this_weeks_chats_and_leads(owner, ai):
    bot, client, key = _live(owner, "capture-leads")
    opened = _post(client, "/api/chat/open/", {"session_key": key}).json()
    quote = opened["expect"]["options"][0]
    _say(client, key, "Get a quote", quote["choice"])
    page = owner.get("/chatbot/").content.decode()
    assert "1</span> chat" in page and "1</span> lead" in page


def test_editor_links_back_to_its_chatbot(owner):
    bot = _create(owner, "answer-questions")
    page = owner.get(f"/automations/{bot.workflow.slug}/").content.decode()
    assert f"/chatbot/{bot.pk}/preview/" in page and "Try in chat" in page

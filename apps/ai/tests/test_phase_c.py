"""Phase C: Anthropic/OpenAI providers, the model router, and replying on its own behind fixed checks."""
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.ai import autonomy, router
from apps.ai.models import AIProposal, AISettings
from apps.ai.providers import AIProviderError, get_ai_provider, model_for
from apps.ai.providers.anthropic import AnthropicProvider
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.providers.openai import OpenAIProvider
from apps.ai.tasks import draft_proposal
from apps.ai.types import ChatMessage
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.core.actions import ActionError

NOW = timezone.now()


def _response(status=200, body=None):
    r = MagicMock(status_code=status)
    r.json.return_value = body if body is not None else {}
    return r


# ---- providers ----
def test_anthropic_request_and_answer():
    body = {"model": "claude-sonnet-5", "content": [{"type": "text", "text": "Hi"}, {"type": "text", "text": " there"}],
            "usage": {"input_tokens": 5, "output_tokens": 2}}
    with patch("apps.ai.providers.http.requests.post", return_value=_response(body=body)) as post:
        out = AnthropicProvider(base_url="https://api.anthropic.com", api_key="sk-test").chat([ChatMessage("user", "hello")], system="Be brief", max_tokens=50)
    assert out.text == "Hi there" and out.usage == {"input_tokens": 5, "output_tokens": 2}
    url, kw = post.call_args[0][0], post.call_args[1]
    assert url == "https://api.anthropic.com/v1/messages"
    assert kw["headers"]["x-api-key"] == "sk-test" and kw["headers"]["anthropic-version"] == "2023-06-01"
    assert kw["json"]["system"] == "Be brief" and kw["json"]["messages"] == [{"role": "user", "content": "hello"}]
    assert kw["json"]["model"] == "claude-sonnet-5" and kw["json"]["max_tokens"] == 50


def test_openai_request_and_answer():
    body = {"model": "gpt-x", "choices": [{"message": {"content": "Hello"}}], "usage": {"prompt_tokens": 3}}
    with patch("apps.ai.providers.http.requests.post", return_value=_response(body=body)) as post:
        out = OpenAIProvider(api_key="sk-o", model="gpt-x").chat([ChatMessage("user", "hi")], system="S")
    assert out.text == "Hello"
    kw = post.call_args[1]
    assert kw["headers"]["Authorization"] == "Bearer sk-o"
    assert kw["json"]["messages"][0] == {"role": "system", "content": "S"}
    assert "max_completion_tokens" in kw["json"] and "temperature" not in kw["json"] and "max_tokens" not in kw["json"]


def test_missing_keys_models_and_http_errors_are_readable():
    with pytest.raises(AIProviderError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(api_key="").chat([ChatMessage("user", "x")])
    with pytest.raises(AIProviderError, match="AI_MODEL"):
        OpenAIProvider(api_key="k", model="").chat([ChatMessage("user", "x")])
    with patch("apps.ai.providers.http.requests.post", return_value=_response(529)):
        with pytest.raises(AIProviderError, match="having problems"):
            AnthropicProvider(api_key="k").chat([ChatMessage("user", "x")])


def test_backends_and_tiers(settings):
    settings.AI_PROVIDER_BACKEND, settings.ANTHROPIC_API_KEY = "anthropic", "k"
    assert isinstance(get_ai_provider(), AnthropicProvider) and get_ai_provider().model == "claude-sonnet-5"
    settings.AI_MODEL_FAST = "claude-haiku-4-5-20251001"
    assert get_ai_provider(tier="fast").model == "claude-haiku-4-5-20251001"
    settings.AI_PROVIDER_BACKEND, settings.AI_MODEL = "openai", "gpt-x"
    assert isinstance(get_ai_provider(), OpenAIProvider) and model_for("standard") == "gpt-x"
    settings.AI_MODEL_FAST = ""
    assert model_for("fast") == "gpt-x", "no fast model set: one model for everything"


def test_router_sends_only_short_simple_messages_to_the_fast_model():
    simple = {"window_open": True, "thread": [{"direction": "inbound", "body": "Are you open today?"}]}
    assert router.choose(simple, has_memory=False)[0] == "fast"
    assert router.choose(simple, has_memory=True)[0] == "standard"
    assert router.choose(dict(simple, window_open=False), has_memory=False)[0] == "standard"
    long_q = {"window_open": True, "thread": [{"direction": "inbound", "body": "x" * 200}]}
    assert router.choose(long_q, has_memory=False)[0] == "standard"


# ---- the facts check, against structured facts ----
FACTS = {
    "opening_hours": {"mon": {"open": "08:00", "close": "17:00"}},
    "products": [{"name": "Solar battery", "price": "4000.00", "currency": "ZMW"}],
    "notes": "3 kW system from K18,000.\nPrices: https://sunrise.example/prices",
}


@pytest.mark.parametrize("reply,ok", [
    ("Our 3 kW system is K18,000 installed.", True),      # amount the owner wrote
    ("The battery is K4,000.", True),                      # catalogue price, written differently
    ("The battery is ZMW 4000.00.", True),
    ("It's K5,000.", False),                               # invented price
    ("It's K500.", False),                                 # "500" isn't a price, even though 5 and 00 appear
    ("We open at 8am and close at 5 pm.", True),           # opening hours, written differently
    ("We close at 18:00.", False),                         # invented time
    ("See https://sunrise.example/prices", True),
    ("See https://elsewhere.example", False),
    ("Delivery takes 7 days.", False),                     # a number nobody wrote
    ("Hello! How can we help?", True),
])
def test_every_price_time_number_and_link_must_come_from_the_business(reply, ok):
    assert (autonomy.unsupported_facts(reply, FACTS) == []) is ok


def test_a_customers_own_numbers_never_count():
    assert autonomy.unsupported_facts("Yes, K5,000.", FACTS, extra_text="") == ["K5,000"]


# ---- replying on its own ----
class Fake(AIProvider):
    answer = ""

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        return CompletionResult(text=Fake.answer, model="fake-3")


def answer(text="We're open until 17:00 today.", intent="hours", confidence=0.95, action="reply"):
    payload = {"text": text} if action == "reply" else {"note": "x"}
    return json.dumps({"version": 1, "action": action, "intent": intent, "confidence": confidence,
                       "reason": "r", "payload": payload})


@pytest.fixture(autouse=True)
def fake(settings, monkeypatch):
    settings.AI_PROVIDER_BACKEND = "ollama"
    monkeypatch.setattr("apps.ai.agent.get_ai_provider", lambda *a, **k: Fake())
    Fake.answer = answer()
    cache.clear()


@pytest.fixture
def sent(monkeypatch):
    from apps.core import actions

    calls, real = [], actions.run_action

    def run_action(name, ctx, **kw):
        if name != "reply":
            return real(name, ctx, **kw)
        calls.append((name, kw))
        return {"outbound_message_id": 1}

    monkeypatch.setattr("apps.core.actions.run_action", run_action)
    return calls


@pytest.fixture
def account(db):
    account = Account.objects.create(company_name="Sunrise Solar")
    AISettings.objects.create(account=account, enabled=True, business_notes="Open 08:00-17:00. 3 kW from K18,000.",
                              reply_mode="auto", auto_topics=["hours", "price"], auto_min_confidence=0.85)
    return account


def convo(account, phone="+260971000001"):
    contact = Contact.objects.create(account=account, phone=phone, first_name="Sam")
    return Conversation.objects.create(account=account, contact=contact, channel="email", last_message_at=NOW)


def draft(conversation, body="Are you open?", requested_by=None):
    m = Message.objects.create(account=conversation.account, conversation=conversation,
                               direction=Message.Direction.INBOUND, body=body, timestamp=timezone.now())
    p = AIProposal.objects.create(account=conversation.account, conversation=conversation,
                                  trigger_message=m, requested_by=requested_by)
    result = draft_proposal.apply(args=(p.pk,)).result
    p.refresh_from_db()
    return p, result


@pytest.mark.django_db
def test_an_allowed_simple_question_is_answered_on_its_own(account, sent):
    c = convo(account)
    p, result = draft(c)
    assert result == "sent" and p.status == "used" and p.auto_sent_at and p.edited_before_send is False
    assert sent == [("reply", {"conversation": c, "body": "We're open until 17:00 today.",
                               "idempotency_key": f"ai-auto:{p.pk}"})]
    assert p.auto_decision["send"] is True and all(chk["ok"] for chk in p.auto_decision["checks"])
    assert p.intent == "hours"


@pytest.mark.parametrize("change,why", [
    ({"intent": "location"}, "isn't on your list"),
    ({"intent": "price"}, "can't be checked yet"),              # allowed, but no catalogue yet
    ({"confidence": 0.6}, "wasn't sure enough for “Careful”"),
    ({"text": "We're open until 17:00, and it's K5,000."}, "K5,000"),
    ({"text": "We're open until 19:00 today."}, "19:00"),
    ({"action": "handoff"}, "need a person"),
])
@pytest.mark.django_db
def test_anything_that_fails_a_check_stays_a_suggestion(account, sent, change, why):
    Fake.answer = answer(**change)
    p, result = draft(convo(account))
    assert result == "ready" and p.status == "ready" and sent == []
    assert p.auto_decision["send"] is False
    from apps.ai import api as ai_api
    assert why in ai_api.serialize(p, p.conversation)["autoNote"]


@pytest.mark.django_db
def test_prices_unlock_with_a_catalogue_and_must_match_it(account, sent):
    from apps.commerce.models import Product

    Product.objects.create(account=account, name="Solar battery", slug="battery", price=4000, currency="ZMW")
    Fake.answer = answer("The solar battery is K4,000.", intent="price")
    assert draft(convo(account))[1] == "sent"
    Fake.answer = answer("The solar battery is K3,500.", intent="price")
    assert draft(convo(account, "+260971000002"))[1] == "ready"


@pytest.mark.django_db
def test_the_percentages_never_reach_the_owner(account, sent):
    Fake.answer = answer(confidence=0.6)
    p, _ = draft(convo(account))
    from apps.ai import api as ai_api
    note = ai_api.serialize(p, p.conversation)["autoNote"]
    assert "%" not in note and "0.6" not in note
    assert next(c for c in p.auto_decision["checks"] if c["name"] == "confident")["value"] == {
        "confidence": 0.6, "needed": 0.85}, "the numbers stay in the audit record"


@pytest.mark.django_db
def test_no_automatic_reply_when_a_teammate_owns_it_asked_for_it_or_it_is_suggest_only(account, sent, settings):
    user = User.objects.create_user("t", "t@example.com", "pw")
    c = convo(account)
    c.assigned_to = user
    c.save()
    assert draft(c)[1] == "ready"
    assert draft(convo(account, "+260971000002"), requested_by=user)[1] == "ready"
    settings.AI_AUTONOMY_ENABLED = False
    assert draft(convo(account, "+260971000003"))[1] == "ready"
    settings.AI_AUTONOMY_ENABLED = True
    AISettings.objects.filter(account=account).update(reply_mode="suggest")
    p, result = draft(convo(account, "+260971000004"))
    assert result == "ready" and p.auto_decision == {}
    assert sent == []


def team_reply(c, minutes_ago, by_ai=False):
    Message.objects.create(account=c.account, conversation=c, direction=Message.Direction.OUTBOUND, body="ok",
                           timestamp=timezone.now() - timedelta(minutes=minutes_ago), status="sent",
                           metadata={"sent_by": "ai"} if by_ai else {})


@pytest.mark.django_db
def test_ai_stays_out_right_after_the_team_replied(account, sent):
    c = convo(account)
    team_reply(c, minutes_ago=3)
    p, result = draft(c)
    assert result == "ready" and "replied a few minutes ago" in p.auto_decision["checks"][7]["detail"]
    c2 = convo(account, "+260971000002")
    team_reply(c2, minutes_ago=30)
    assert draft(c2)[1] == "sent"
    c3 = convo(account, "+260971000003")
    team_reply(c3, minutes_ago=1, by_ai=True)   # AI's own reply doesn't count as the team's
    assert draft(c3)[1] == "sent"


@pytest.mark.django_db
def test_a_person_takes_over_after_five_automatic_replies_in_a_row(account, sent):
    c = convo(account)
    results = [draft(c, f"open? {i}")[1] for i in range(6)]
    assert results == ["sent"] * 5 + ["ready"]
    AIProposal.objects.filter(auto_sent_at__isnull=False).update(auto_sent_at=timezone.now() - timedelta(hours=1))
    team_reply(c, minutes_ago=15)                # then the team speaks: the count starts again
    assert draft(c, "and on Sunday?")[1] == "sent"


@pytest.mark.django_db
def test_one_automatic_reply_per_customer_message(account, sent):
    from apps.ai import autonomy as a

    c = convo(account)
    p, _ = draft(c)
    again = a.evaluate(ai_settings=AISettings.objects.get(account=account), proposal={
        "action": "reply", "intent": "hours", "confidence": 0.95, "payload": {"text": "Open until 17:00."}},
        conversation=c, automatic=True, window_open=True, facts={"notes": "Open 08:00-17:00", "products": []},
        trigger_message_id=p.trigger_message_id)
    assert not again.send and again.first_failure["name"] == "one_per_message"


@pytest.mark.django_db
def test_only_when_closed(account, sent, monkeypatch):
    AISettings.objects.filter(account=account).update(auto_only_when_closed=True)
    monkeypatch.setattr("apps.accounts.business_hours.is_open", lambda *a, **k: True)
    assert draft(convo(account))[1] == "ready"
    monkeypatch.setattr("apps.accounts.business_hours.is_open", lambda *a, **k: False)
    assert draft(convo(account, "+260971000002"))[1] == "sent"


@pytest.mark.django_db
def test_a_failed_send_falls_back_to_a_suggestion(account, monkeypatch):
    from apps.core import actions

    real = actions.run_action

    def refuse(name, ctx, **k):
        if name != "reply":
            return real(name, ctx, **k)
        raise ActionError("The 24-hour window has closed.")

    monkeypatch.setattr("apps.core.actions.run_action", refuse)
    p, result = draft(convo(account))
    assert result == "ready" and p.status == "ready" and p.auto_sent_at is None
    assert "24-hour" in p.auto_decision["error"]


# ---- settings and inbox ----
@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, user


@pytest.mark.django_db
def test_the_owner_chooses_automatic_replies_and_topics(owner, account):
    client, user = owner
    AISettings.objects.filter(account=account).update(reply_mode="suggest", auto_topics=[])
    client.post("/settings/ai/", {"enabled": "on", "business_notes": "n", "reply_mode": "auto",
                                  "auto_topics": ["hours", "price", "delivery", "hack_the_planet"],
                                  "auto_min_confidence": "0.5"})
    s = AISettings.objects.get(account=account)
    assert (s.reply_mode, s.auto_topics, s.auto_min_confidence) == ("auto", ["hours"], 0.85), \
        "price is locked without a catalogue, delivery until it's structured"
    assert s.auto_consented_by == user and s.auto_consented_at
    html = client.get("/settings/ai/").content.decode()
    assert "Reply automatically to the questions I choose" in html and "Opening hours and whether you" in html

    client.post("/settings/ai/", {"enabled": "on", "business_notes": "n", "reply_mode": "suggest"})
    assert AISettings.objects.get(account=account).reply_mode == "suggest"


@pytest.mark.django_db
def test_the_inbox_labels_replies_ai_sent_and_settings_lists_them(owner, account, sent):
    client, _ = owner
    c = convo(account)
    draft(c)
    Message.objects.create(account=account, conversation=c, direction=Message.Direction.OUTBOUND,
                           body="We're open until 17:00 today.", timestamp=timezone.now() + timedelta(seconds=1),
                           status="sent", metadata={"sent_by": "ai"})
    feed = client.get(f"/inbox/{c.public_id}/messages/").json()
    assert [m["byAi"] for m in feed["messages"] if m["direction"] == "outbound"] == [True]
    assert "Sent by AI" in client.get(f"/inbox/{c.public_id}/").content.decode()
    html = client.get("/settings/ai/").content.decode()
    assert "Recent automatic replies" in html and "We&#x27;re open until 17:00 today." in html


@pytest.mark.django_db
def test_the_autopilot_report_counts_and_explains_each_decision(owner, account, sent):
    client, _ = owner
    draft(convo(account))                                         # sent
    Fake.answer = answer(confidence=0.5)
    draft(convo(account, "+260971000002"))                        # held: not sure
    Fake.answer = answer("We're open until 21:00.")
    held, _ = draft(convo(account, "+260971000003"))              # held: facts
    from apps.ai import api as ai_api
    ai_api.record_used(account, held.pk, "We're open until 17:00.")  # the team fixed and sent it
    report = ai_api.autopilot_report(account)
    assert (report["sent"], report["held"], report["reviewed"]) == (1, 2, 1)
    assert dict(report["reasons"]) == {"AI wasn't sure": 1, "Facts couldn't be checked": 1}
    html = client.get("/settings/ai/autopilot/").content.decode()
    assert "Automatic replies sent" in html and "Held: Facts couldn" in html and "21:00" in html

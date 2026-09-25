"""Phase B: look-ups, conversation memory and one-click extras. AI still never sends or changes anything itself."""
import json
from datetime import datetime, timedelta, timezone as dt_timezone

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from apps.accounts import business_hours
from apps.accounts.models import Account, Membership
from apps.ai import memory as ai_memory
from apps.ai import proposals
from apps.ai.models import AIConversationMemory, AIProposal, AISettings
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.tasks import draft_proposal, refresh_memory
from apps.billing.models import ModuleSubscription
from apps.commerce.models import Product
from apps.contacts.models import Contact, Tag
from apps.conversations.models import Conversation, FollowUp, Message
from apps.core.actions import ActionError, run_action
from apps.crm.models import Lead

NOW = timezone.now()


def reply(text="Happy to help.", **extra):
    return json.dumps({"version": 1, "action": "reply", "confidence": 0.8, "reason": "r",
                       "payload": {"text": text}, **extra})


class ScriptedProvider(AIProvider):
    """Answers from a script, one entry per call, and remembers what it was sent."""
    script, calls = [], []

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        ScriptedProvider.calls.append({"messages": list(messages), "system": system})
        return CompletionResult(text=ScriptedProvider.script.pop(0), model="fake-2")


@pytest.fixture(autouse=True)
def scripted(settings, monkeypatch):
    settings.AI_PROVIDER_BACKEND = "ollama"
    monkeypatch.setattr("apps.ai.agent.get_ai_provider", lambda account=None: ScriptedProvider())
    monkeypatch.setattr("apps.ai.providers.get_ai_provider", lambda account=None: ScriptedProvider())
    ScriptedProvider.script, ScriptedProvider.calls = [], []
    cache.clear()


@pytest.fixture
def account(db):
    account = Account.objects.create(company_name="Sunrise Solar")
    AISettings.objects.create(account=account, enabled=True, business_notes="3 kW from K18,000.")
    return account


def convo(account, phone="+260971000001"):
    contact = Contact.objects.create(account=account, phone=phone, first_name="Sam")
    return Conversation.objects.create(account=account, contact=contact, channel="email", last_message_at=NOW)


def say(conversation, body, direction=Message.Direction.INBOUND, minutes_ago=0):
    return Message.objects.create(account=conversation.account, conversation=conversation, direction=direction,
                                  body=body, timestamp=NOW - timedelta(minutes=minutes_ago))


def draft_for(conversation, body="Are you open now?"):
    m = say(conversation, body)
    p = AIProposal.objects.create(account=conversation.account, conversation=conversation, trigger_message=m)
    draft_proposal.apply(args=(p.pk,))
    p.refresh_from_db()
    return p


# ---- look-ups (read-only registry actions) ----
@pytest.mark.django_db
def test_opening_hours_say_when_the_business_next_opens(account):
    business_hours.save_hours(account, tz="Africa/Lusaka", schedule={
        "mon": {"open": "08:00", "close": "17:00"}, "tue": {"open": "08:00", "close": "17:00"}})
    monday_night = datetime(2026, 9, 21, 19, 0, tzinfo=dt_timezone.utc)  # 21:00 in Lusaka
    info = business_hours.availability(account, monday_night)
    assert info["open_now"] is False and info["next_open"] == "tomorrow at 08:00" and info["today"] == "08:00-17:00"
    assert business_hours.availability(Account.objects.create(company_name="No hours"))["hours_set"] is False


@pytest.mark.django_db
def test_look_ups_only_ever_see_the_callers_own_business(account):
    other = Account.objects.create(company_name="Other")
    theirs = convo(other, "+260971000009")
    with pytest.raises(ActionError):
        run_action("lookup_customer", {"account": account}, conversation=theirs)
    Product.objects.create(account=other, name="Solar panel", slug="panel", price=900)
    Product.objects.create(account=account, name="Solar battery", slug="battery", price=4000, currency="ZMW")
    found = run_action("lookup_products", {"account": account}, account=account, query="solar")
    assert found == {"products": [{"name": "Solar battery", "price": "4000.00", "currency": "ZMW"}]}
    ModuleSubscription.objects.create(account=account, module=ModuleSubscription.COMMERCE, enabled=False)
    with pytest.raises(ActionError, match="module"):
        run_action("lookup_products", {"account": account}, account=account, query="solar")


@pytest.mark.django_db
def test_the_model_can_look_something_up_before_proposing(account):
    c = convo(account)
    ScriptedProvider.script = ['{"tool": "check_opening_hours", "args": {}}', reply("Yes, we're open until 17:00.")]
    p = draft_for(c)
    assert p.status == "ready" and p.tools_used == ["check_opening_hours"]
    second = ScriptedProvider.calls[1]["messages"]
    assert second[-1].role == "user" and second[-1].content.startswith("Result of check_opening_hours")
    assert "check_opening_hours" in ScriptedProvider.calls[0]["system"]
    assert Message.objects.filter(direction="outbound").count() == 0, "AI must never send"


@pytest.mark.django_db
def test_an_unknown_look_up_is_answered_with_an_error_not_a_crash(account):
    c = convo(account)
    ScriptedProvider.script = ['{"tool": "read_bank_balance"}', reply()]
    p = draft_for(c)
    assert p.status == "ready" and "no look-up called" in ScriptedProvider.calls[1]["messages"][-1].content


@pytest.mark.django_db
def test_endless_look_ups_are_cut_off(account):
    c = convo(account)
    ScriptedProvider.script = ['{"tool": "get_customer"}'] * 4
    p = draft_for(c)
    assert p.status == "error" and "look-ups" in p.error and len(ScriptedProvider.calls) == 4


@pytest.mark.django_db
def test_products_look_up_is_not_offered_without_the_commerce_module(account):
    ModuleSubscription.objects.create(account=account, module=ModuleSubscription.COMMERCE, enabled=False)
    ScriptedProvider.script = [reply()]
    draft_for(convo(account))
    system = ScriptedProvider.calls[0]["system"]
    assert "search_products" not in system and "check_opening_hours" in system


# ---- extras: suggested, never applied by AI ----
def test_extras_keep_only_known_tags_sensible_follow_ups_and_at_most_three():
    extras = proposals.clean_extras([
        {"kind": "tag", "tag": "VIP"}, {"kind": "tag", "tag": "made-up"}, {"kind": "track_interest"},
        {"kind": "follow_up", "in_days": 40}, {"kind": "follow_up", "in_days": "2", "note": "quote"},
        {"kind": "delete_customer"}, {"kind": "tag", "tag": "vip"},
    ], tags=["vip"], can_track=False)
    assert extras == [{"kind": "tag", "tag": "vip"}, {"kind": "follow_up", "in_days": 2, "note": "quote"}]


@pytest.fixture
def agent(client, account):
    user = User.objects.create_user("agent", "agent@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_a_person_applies_extras_with_one_click(agent, account):
    Tag.objects.create(account=account, name="solar-quote", slug="solar-quote")
    c = convo(account)
    ScriptedProvider.script = [reply(extras=[
        {"kind": "tag", "tag": "solar-quote"}, {"kind": "track_interest"},
        {"kind": "follow_up", "in_days": 2, "note": "Check they got the quote"}])]
    p = draft_for(c, "How much for 3 kW?")
    assert [e["kind"] for e in p.extras] == ["tag", "track_interest", "follow_up"]
    assert not c.contact.tags.exists() and not FollowUp.objects.exists(), "nothing happens until a person clicks"

    feed = agent.get(f"/inbox/{c.public_id}/messages/").json()["aiProposal"]
    assert [e["label"] for e in feed["extras"]] == [
        "Tag “solar-quote”", "Track as interested", "Follow up in 2 days: Check they got the quote"]

    url = f"/inbox/{c.public_id}/ai/apply/"
    for i in range(3):
        assert agent.post(url, {"proposal": p.pk, "index": i}).json()["ok"]
    assert list(c.contact.tags.values_list("name", flat=True)) == ["solar-quote"]
    assert Lead.objects.filter(contact=c.contact).count() == 1
    assert FollowUp.objects.get().note == "Check they got the quote"
    assert agent.post(url, {"proposal": p.pk, "index": 1}).json()["message"] == "Already done."
    assert Lead.objects.filter(contact=c.contact).count() == 1


@pytest.mark.django_db
def test_extras_cant_be_applied_through_another_conversation(agent, account):
    c, other = convo(account), convo(account, "+260971000002")
    ScriptedProvider.script = [reply(extras=[{"kind": "follow_up", "in_days": 1}])]
    p = draft_for(c)
    assert agent.post(f"/inbox/{other.public_id}/ai/apply/", {"proposal": p.pk, "index": 0}).status_code == 400
    assert not FollowUp.objects.exists()


# ---- conversation memory ----
def long_thread(c, n=20):
    for i in range(n):
        say(c, f"Message {i}: call me on 0971234567" if i == 0 else f"Message {i}",
            direction=Message.Direction.INBOUND if i % 2 == 0 else Message.Direction.OUTBOUND, minutes_ago=100 - i)


@pytest.mark.django_db
def test_a_long_conversation_is_folded_into_memory_and_used_next_time(account, monkeypatch):
    c = convo(account)
    long_thread(c)
    queued = []
    monkeypatch.setattr(refresh_memory, "apply_async", lambda args, **kw: queued.append(args))
    ScriptedProvider.script = [reply()]
    draft_for(c)
    assert queued == [(c.pk,)], "enough older messages: a refresh is queued after the draft"

    ScriptedProvider.script = [json.dumps({"summary": "Wants a 3 kW system; ring 0971234567.",
                                           "facts": {"wants": "3 kW system", "town": "Kitwe"}})]
    assert refresh_memory.apply(args=(c.pk,)).result == "updated"
    sent = ScriptedProvider.calls[-1]["messages"][0].content
    assert "Message 0" in sent and "0971234567" not in sent, "older messages only, masked"
    assert "Message 19" not in sent, "the recent window is never summarised"
    mem = AIConversationMemory.objects.get(conversation=c)
    assert "0971234567" not in mem.summary and mem.facts == {"wants": "3 kW system", "town": "Kitwe"}
    assert not ai_memory.needs_refresh(c)

    ScriptedProvider.script = [reply()]
    draft_for(c, "So when can you install?")
    system = ScriptedProvider.calls[-1]["system"]
    assert "Wants a 3 kW system" in system and "town: Kitwe" in system


@pytest.mark.django_db
def test_short_conversations_are_not_summarised(account, monkeypatch):
    c = convo(account)
    long_thread(c, n=6)
    queued = []
    monkeypatch.setattr(refresh_memory, "apply_async", lambda *a, **k: queued.append(1))
    ScriptedProvider.script = [reply()]
    draft_for(c)
    assert queued == [] and not AIConversationMemory.objects.exists()

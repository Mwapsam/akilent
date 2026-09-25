"""From a customer's message to a proposal a person can use. AI never sends anything itself."""
import json
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.ai import api as ai_api
from apps.ai import prompts
from apps.ai.models import AIProposal, AISettings
from apps.ai.providers import AIProviderError
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.tasks import draft_proposal
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.conversations.signals import conversation_message_processed

NOW = timezone.now()
PRICE_ANSWER = json.dumps({"version": 1, "action": "reply", "confidence": 0.9, "reason": "Asked price",
                           "payload": {"text": "Our 3 kW system starts at K18,000."}})


class FakeProvider(AIProvider):
    calls = []
    answer = PRICE_ANSWER
    fail = None

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        FakeProvider.calls.append({"messages": messages, "system": system})
        if FakeProvider.fail:
            raise FakeProvider.fail
        return CompletionResult(text=FakeProvider.answer, model="fake-1")


@pytest.fixture(autouse=True)
def fake_ai(settings, monkeypatch):
    # Configured as Ollama, but every call goes to the fake: nothing leaves the machine.
    settings.AI_PROVIDER_BACKEND = "ollama"
    monkeypatch.setattr("apps.ai.agent.get_ai_provider", lambda *a, **k: FakeProvider())
    FakeProvider.calls, FakeProvider.fail, FakeProvider.answer = [], None, PRICE_ANSWER
    cache.clear()
    yield


@pytest.fixture
def account(db):
    account = Account.objects.create(company_name="Sunrise Solar")
    AISettings.objects.create(account=account, enabled=True, business_notes="3 kW system from K18,000.")
    return account


def convo(account, phone="+260971000001"):
    contact = Contact.objects.create(account=account, phone=phone, first_name="Sam", email=f"{phone[-3:]}@example.com")
    return Conversation.objects.create(account=account, contact=contact, channel="email", last_message_at=NOW)


def say(conversation, body, direction=Message.Direction.INBOUND, minutes_ago=0):
    return Message.objects.create(account=conversation.account, conversation=conversation, direction=direction,
                                  body=body, timestamp=NOW - timedelta(minutes=minutes_ago))


def announce(conversation, message, handled=False):
    return conversation_message_processed.send_robust(
        sender=Conversation, conversation=conversation, message=message, handled_by_automation=handled)


def draft(proposal_id):
    draft_proposal.apply(args=(proposal_id,))


@pytest.mark.django_db
def test_a_customer_message_becomes_one_ready_proposal(account, django_capture_on_commit_callbacks, monkeypatch):
    queued = []
    monkeypatch.setattr(draft_proposal, "apply_async", lambda args, **kw: queued.append((args, kw)))
    c = convo(account)
    m = say(c, "How much is the solar package?")
    with django_capture_on_commit_callbacks(execute=True):
        announce(c, m)
        announce(c, m)  # a replayed message reuses its proposal
    assert len(queued) == 1 and queued[0][1]["queue"] == "ai" and queued[0][1]["countdown"] == 3
    draft(queued[0][0][0])
    p = AIProposal.objects.get()
    assert (p.status, p.action, p.payload["text"], p.model) == (
        "ready", "reply", "Our 3 kW system starts at K18,000.", "fake-1")
    assert p.trigger_message == m and p.reason == "Asked price" and p.latency_ms is not None
    assert Message.objects.filter(direction="outbound").count() == 0, "AI must never send"


@pytest.mark.django_db
def test_nothing_is_queued_when_an_automation_answered_or_ai_is_off(account, settings, monkeypatch):
    queued = []
    monkeypatch.setattr(draft_proposal, "apply_async", lambda *a, **k: queued.append(1))
    c = convo(account)
    announce(c, say(c, "price?"), handled=True)
    AISettings.objects.filter(account=account).update(enabled=False)
    announce(c, say(c, "price again?"))
    AISettings.objects.filter(account=account).update(enabled=True)
    settings.AI_PROVIDER_BACKEND = "none"
    announce(c, say(c, "and again?"))
    assert queued == [] and not AIProposal.objects.exists()


@pytest.mark.django_db
def test_the_plan_can_switch_ai_off(account):
    from apps.billing.models import ModuleSubscription

    ModuleSubscription.objects.create(account=account, module=ModuleSubscription.AI, enabled=False)
    assert ai_api.is_available(account) is False
    assert "module" in ai_api.unavailable_reason(account)


@pytest.mark.django_db
def test_settings_page_says_why_ai_is_off(agent, account):
    from apps.billing.models import ModuleSubscription

    assert "AI is on." in agent.get("/settings/ai/").content.decode()
    ModuleSubscription.objects.create(account=account, module=ModuleSubscription.AI, enabled=False)
    assert "AI module is switched off" in agent.get("/settings/ai/").content.decode()


@pytest.mark.django_db
def test_a_burst_of_messages_gets_one_answer(account):
    c = convo(account)
    first = say(c, "Hi", minutes_ago=1)
    p1 = AIProposal.objects.create(account=account, conversation=c, trigger_message=first)
    second = say(c, "How much?")
    p2 = AIProposal.objects.create(account=account, conversation=c, trigger_message=second)
    draft(p1.pk)
    draft(p2.pk)
    p1.refresh_from_db()
    p2.refresh_from_db()
    assert (p1.status, p2.status) == ("expired", "ready")
    assert len(FakeProvider.calls) == 1


@pytest.mark.django_db
def test_no_proposal_once_someone_has_replied(account):
    c = convo(account)
    m = say(c, "How much?", minutes_ago=1)
    p = AIProposal.objects.create(account=account, conversation=c, trigger_message=m)
    say(c, "K18,000", direction=Message.Direction.OUTBOUND)
    draft(p.pk)
    p.refresh_from_db()
    assert p.status == "expired" and FakeProvider.calls == []


@pytest.mark.django_db
def test_provider_failure_retries_then_records_a_readable_error(account):
    from celery.exceptions import Retry

    FakeProvider.fail = AIProviderError("The AI service took too long to answer.")
    c = convo(account)
    p = AIProposal.objects.create(account=account, conversation=c, trigger_message=say(c, "hello?"))
    with pytest.raises(Retry):
        draft_proposal.apply(args=(p.pk,), throw=True)  # first attempt: retried later
    p.refresh_from_db()
    assert p.status == "pending"
    assert cache.get(f"ai-lock:conversation:{c.pk}") is None, "a retry must not leave the conversation locked"
    draft_proposal.apply(args=(p.pk,), retries=2)  # the last attempt records the failure
    p.refresh_from_db()
    assert p.status == "error" and "too long" in p.error


@pytest.mark.django_db
def test_an_unusable_answer_is_recorded_not_raised(account):
    FakeProvider.answer = "I think you should say hi"
    c = convo(account)
    p = AIProposal.objects.create(account=account, conversation=c, trigger_message=say(c, "hello?"))
    draft(p.pk)
    p.refresh_from_db()
    assert p.status == "error" and "format" in p.error


@pytest.mark.django_db
def test_the_daily_limit_stops_calls(account, settings):
    settings.AI_DAILY_CALL_LIMIT = 1
    c = convo(account)
    for body in ("one", "two"):
        m = say(c, body)
        draft(AIProposal.objects.create(account=account, conversation=c, trigger_message=m).pk)
    assert list(AIProposal.objects.order_by("id").values_list("status", flat=True)) == ["ready", "error"]
    assert len(FakeProvider.calls) == 1


@pytest.mark.django_db
def test_the_prompt_holds_only_this_conversation_and_hides_contact_details(account):
    c = convo(account)
    other = convo(account, "+260971000002")
    say(other, "SECRET from another customer")
    say(c, "Automated line", direction=Message.Direction.SYSTEM, minutes_ago=3)
    say(c, "Call me on +260 97 555 1234 or mail sam@example.com", minutes_ago=2)
    m = say(c, "How much is it?", minutes_ago=1)
    draft(AIProposal.objects.create(account=account, conversation=c, trigger_message=m).pk)
    sent = FakeProvider.calls[0]
    everything = sent["system"] + " ".join(msg.content for msg in sent["messages"])
    assert "SECRET" not in everything and "Automated line" not in everything
    assert "555 1234" not in everything and "sam@example.com" not in everything and "[phone]" in everything
    assert "K18,000" in sent["system"] and "Sam" in sent["system"] and "Sunrise Solar" in sent["system"]
    assert [msg.role for msg in sent["messages"]] == ["user", "user"]


def test_mask_keeps_prices_and_small_numbers():
    assert prompts.mask("It costs K18,000 for 3 kW") == "It costs K18,000 for 3 kW"
    assert prompts.mask("ring 0971234567") == "ring [phone]"


# ---- inbox ----
@pytest.fixture
def agent(client, account):
    user = User.objects.create_user("agent", "agent@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_the_feed_carries_the_ready_proposal(agent, account):
    c = convo(account)
    m = say(c, "How much?")
    draft(AIProposal.objects.create(account=account, conversation=c, trigger_message=m).pk)
    data = agent.get(f"/inbox/{c.public_id}/messages/").json()
    assert data["aiProposal"]["text"] == "Our 3 kW system starts at K18,000."
    html = agent.get(f"/inbox/{c.public_id}/").content.decode()
    assert "Suggested reply" in html and '"enabled": true' in html


@pytest.mark.django_db
def test_no_ai_anywhere_in_the_inbox_when_it_is_off(agent, account, settings):
    settings.AI_PROVIDER_BACKEND = "none"
    c = convo(account)
    say(c, "hi")
    assert agent.get(f"/inbox/{c.public_id}/messages/").json()["aiProposal"] is None
    assert '"enabled": false' in agent.get(f"/inbox/{c.public_id}/").content.decode()
    assert agent.post(f"/inbox/{c.public_id}/ai/suggest/").status_code == 400


@pytest.mark.django_db
def test_suggest_dismiss_and_use_are_recorded(agent, account, monkeypatch):
    monkeypatch.setattr(draft_proposal, "apply_async", lambda *a, **k: None)
    c = convo(account)
    say(c, "How much?")
    first = agent.post(f"/inbox/{c.public_id}/ai/suggest/").json()["proposal"]
    assert first["status"] == "pending"
    draft(first["id"])
    agent.post(f"/inbox/{c.public_id}/ai/dismiss/", {"proposal": first["id"]})
    assert AIProposal.objects.get(pk=first["id"]).status == "dismissed"

    second = agent.post(f"/inbox/{c.public_id}/ai/suggest/").json()["proposal"]
    draft(second["id"])
    ai_api.record_used(account, second["id"], "Our 3 kW system starts at K18,000!")
    used = AIProposal.objects.get(pk=second["id"])
    assert used.status == "used" and used.used_at and used.edited_before_send is True


@pytest.mark.django_db
def test_another_businesses_conversation_is_refused(agent, account):
    other = Account.objects.create(company_name="Other")
    AISettings.objects.create(account=other, enabled=True)
    theirs = convo(other, "+260971000009")
    assert agent.post(f"/inbox/{theirs.public_id}/ai/suggest/").status_code == 404
    assert not AIProposal.objects.exists()


@pytest.mark.django_db
def test_settings_page_records_consent(agent, account):
    AISettings.objects.filter(account=account).delete()
    agent.post("/settings/ai/", {"enabled": "on", "business_notes": "Open 8-5."})
    s = AISettings.objects.get(account=account)
    assert s.enabled and s.consented_at and s.business_notes == "Open 8-5."
    assert "What is sent to the AI service" in agent.get("/settings/ai/").content.decode()


@pytest.mark.django_db
def test_a_broken_ai_receiver_never_affects_recording_a_message(account, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("AI exploded")

    monkeypatch.setattr(ai_api, "is_available", boom)
    c = convo(account)
    m = say(c, "hello")
    results = announce(c, m)  # must not raise
    assert any(isinstance(r, Exception) for _, r in results)
    assert Message.objects.filter(pk=m.pk).exists()

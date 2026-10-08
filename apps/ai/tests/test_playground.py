"""The knowledge playground: an owner asks a customer question and sees what AI would do.

The same prompt, knowledge and checks as a real conversation, run as an ``AIDraft`` of kind
"playground". Nothing is sent and no conversation or proposal is created.
"""

import json

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache

from apps.accounts.models import Account, Membership
from apps.ai.autonomy import DEFAULT_HOLDING_REPLY
from apps.ai.models import AIDraft, AIProposal, AISettings, KnowledgeBaseEntry
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.tasks import build_draft
from apps.conversations.models import Conversation, Message

JOIN_Q = "How can I join TaskCentro as a freelancer?"
JOIN_A = "Create a TaskCentro account, then set up your freelancer profile."


class Fake(AIProvider):
    answer, systems, tiers = "", [], []

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        Fake.systems.append(system)
        return CompletionResult(text=Fake.answer, model="fake-p")


def answer(text=JOIN_A, intent="faq", confidence=0.95, action="reply", sources=()):
    payload = {"text": text} if action == "reply" else {"note": "Needs a person."}
    return json.dumps(
        {
            "version": 1,
            "action": action,
            "intent": intent,
            "confidence": confidence,
            "sources": list(sources),
            "reason": "r",
            "payload": payload,
        }
    )


@pytest.fixture(autouse=True)
def fake(settings, monkeypatch):
    settings.AI_PROVIDER_BACKEND = "ollama"

    def provider(*a, tier="standard", **k):
        Fake.tiers.append(tier)
        return Fake()

    monkeypatch.setattr("apps.ai.providers.get_ai_provider", provider)
    # The playground routes like a real conversation: the agent picks the provider and tier.
    monkeypatch.setattr("apps.ai.agent.get_ai_provider", provider)
    Fake.tiers = []
    Fake.answer, Fake.systems = answer(), []
    cache.clear()


@pytest.fixture
def account(db):
    account = Account.objects.create(company_name="TaskCentro")
    AISettings.objects.create(
        account=account,
        enabled=True,
        reply_mode="auto",
        auto_topics=["faq"],
        auto_min_confidence=0.85,
    )
    return account


@pytest.fixture
def entry(account):
    return KnowledgeBaseEntry.objects.create(
        account=account, title=JOIN_Q, content=JOIN_A
    )


def ask(account, question=JOIN_Q, channel="whatsapp"):
    d = AIDraft.objects.create(
        account=account,
        kind=AIDraft.Kind.PLAYGROUND,
        prompt=question,
        context={"channel": channel},
    )
    build_draft.apply(args=(d.pk,))
    d.refresh_from_db()
    return d


@pytest.mark.django_db
def test_a_question_the_knowledge_base_answers_would_be_sent(account, entry):
    Fake.answer = answer(sources=[f"k{entry.pk}"])
    d = ask(account, channel="instagram")
    assert d.status == "ready", d.error
    r = d.result
    assert r["action"] == "reply" and r["text"] == JOIN_A
    assert r["intent"] == "faq" and r["confidence"] == 95
    assert r["sources"] == [{"id": f"k{entry.pk}", "title": JOIN_Q}]
    assert r["would_send"] is True and r["why"] == "Every check passes."
    assert r["holding"] == ""
    assert "on behalf of TaskCentro on Instagram" in Fake.systems[-1]
    # Nothing real happened.
    assert not AIProposal.objects.exists()
    assert not Conversation.objects.exists() and not Message.objects.exists()


@pytest.mark.django_db
def test_an_answer_without_a_source_says_why_and_what_the_customer_would_get(
    account, entry
):
    Fake.answer = answer(sources=[])
    r = ask(account).result
    assert r["would_send"] is False
    assert r["why"] == "No entry in your knowledge base backs this answer."
    assert r["holding"] == DEFAULT_HOLDING_REPLY


@pytest.mark.django_db
def test_a_refund_question_goes_to_a_person(account, entry):
    Fake.answer = answer(text="Our refund policy is...", sources=[f"k{entry.pk}"])
    r = ask(account, question="I want a refund").result
    assert r["would_send"] is False and "refund" in r["why"]


@pytest.mark.django_db
def test_in_suggest_mode_it_says_automatic_replies_are_off(account, entry):
    AISettings.objects.filter(account=account).update(reply_mode="suggest")
    Fake.answer = answer(sources=[f"k{entry.pk}"])
    r = ask(account).result
    assert r["auto_mode"] is False and r["would_send"] is False
    assert r["why"] == "Automatic replies are off." and r["holding"] == ""


@pytest.mark.django_db
def test_the_owner_asks_from_the_knowledge_page(client, account, entry):
    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)

    page = client.get("/settings/ai/knowledge/").content.decode()
    assert "Try a question" in page and "kind: 'playground'" in page

    empty = client.post("/ai/drafts/", {"kind": "playground", "prompt": " "})
    assert empty.status_code == 400

    r = client.post(
        "/ai/drafts/",
        {"kind": "playground", "prompt": JOIN_Q, "channel": "instagram"},
    )
    assert r.status_code == 200 and r.json()["ok"]
    d = AIDraft.objects.get(pk=r.json()["draft"]["id"])
    assert d.kind == "playground" and d.context == {"channel": "instagram"}


@pytest.mark.django_db
def test_the_preview_uses_the_model_tier_a_customer_would_get(account, entry):
    Fake.answer = answer(sources=[f"k{entry.pk}"])
    ask(account)  # matches a knowledge-base question: the standard model
    ask(account, question="hi")  # short and simple: the fast model, as in a real chat
    assert Fake.tiers[-2:] == ["standard", "fast"]

"""AI answers as the business, from the business's own Q&A, on WhatsApp and Instagram.

The TaskCentro case: a business wrote answers to its common questions, and AI still handed every
one to a person ("outside the scope of Akilent"). Covers the prompt (who AI speaks for, the Q&A it
sees), the "faq" and "sensitive" intents, the grounded check, and the holding reply AI sends once
when it can't answer.
"""

import json
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone

from apps.accounts.models import Account, BusinessKnowledge
from apps.ai import facts as business_facts
from apps.ai import prompts, proposals
from apps.ai.autonomy import DEFAULT_HOLDING_REPLY
from apps.ai.models import AIProposal, AISettings, KnowledgeBaseEntry
from apps.ai.providers.base import AIProvider, CompletionResult
from apps.ai.tasks import draft_proposal
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message

JOIN_Q = "How can I join TaskCentro as a freelancer?"
JOIN_A = "Create a TaskCentro account, then set up your freelancer profile and add your services."


class Fake(AIProvider):
    answer = ""
    systems: list = []

    def chat(self, messages, system="", max_tokens=1024, temperature=0.3, timeout=None):
        Fake.systems.append(system)
        return CompletionResult(text=Fake.answer, model="fake-3")


def answer(text=JOIN_A, intent="faq", confidence=0.95, action="reply", sources=None):
    payload = {"text": text} if action == "reply" else {"note": "x"}
    return json.dumps(
        {
            "version": 1,
            "action": action,
            "intent": intent,
            "confidence": confidence,
            "sources": sources or [],
            "reason": "r",
            "payload": payload,
        }
    )


@pytest.fixture(autouse=True)
def fake(settings, monkeypatch):
    settings.AI_PROVIDER_BACKEND = "ollama"
    monkeypatch.setattr("apps.ai.agent.get_ai_provider", lambda *a, **k: Fake())
    Fake.answer, Fake.systems = answer(), []
    cache.clear()


@pytest.fixture
def sent(monkeypatch):
    from apps.core import actions

    calls, real = [], actions.run_action

    def run_action(name, ctx, **kw):
        if name != "reply":
            return real(name, ctx, **kw)
        calls.append(kw)
        return {"outbound_message_id": 1}

    monkeypatch.setattr("apps.core.actions.run_action", run_action)
    return calls


@pytest.fixture
def account(db):
    account = Account.objects.create(company_name="TaskCentro")
    AISettings.objects.create(
        account=account,
        enabled=True,
        reply_mode="auto",
        auto_topics=["faq", "hours"],
        auto_min_confidence=0.85,
    )
    return account


@pytest.fixture
def join_entry(account):
    # Placed far past the first 30 by title, where the old prompt never looked.
    for i in range(40):
        KnowledgeBaseEntry.objects.create(
            account=account, title=f"A{i:03d} other question", content="Other answer."
        )
    return KnowledgeBaseEntry.objects.create(
        account=account, title=f"Zz {JOIN_Q}", content=JOIN_A
    )


def convo(account, channel="email", phone="+260971000001"):
    contact = Contact.objects.create(account=account, phone=phone, first_name="Sam")
    return Conversation.objects.create(
        account=account,
        contact=contact,
        channel=channel,
        last_message_at=timezone.now(),
    )


def draft(conversation, body=JOIN_Q):
    m = Message.objects.create(
        account=conversation.account,
        conversation=conversation,
        direction=Message.Direction.INBOUND,
        body=body,
        timestamp=timezone.now(),
    )
    p = AIProposal.objects.create(
        account=conversation.account, conversation=conversation, trigger_message=m
    )
    result = draft_proposal.apply(args=(p.pk,)).result
    p.refresh_from_db()
    return p, result


def team_reply(c, minutes_ago):
    Message.objects.create(
        account=c.account,
        conversation=c,
        direction=Message.Direction.OUTBOUND,
        body="Hi, I can help.",
        timestamp=timezone.now() - timedelta(minutes=minutes_ago),
        status="sent",
    )


# ---- the prompt -------------------------------------------------------------------------------


def test_the_prompt_speaks_for_the_business_on_its_channel():
    system, _ = prompts.build(
        business_name="TaskCentro",
        business_notes="",
        hours_text="",
        templates=[],
        customer={},
        thread=[{"direction": "inbound", "body": JOIN_Q}],
        window_open=True,
        structured_facts={
            "knowledge": [{"id": "k7", "title": JOIN_Q, "content": JOIN_A}]
        },
        channel="instagram",
    )
    opening = system.splitlines()[0]
    assert opening.startswith(
        "You reply to customers on behalf of TaskCentro on Instagram."
    )
    assert "WhatsApp" not in system
    assert "### Questions and answers" in system
    assert f"[k7] Q: {JOIN_Q}\nA: {JOIN_A}" in system
    # Q&A is explained in the model's own words, not dumped into the exact-facts JSON.
    assert '"knowledge"' not in system


@pytest.mark.django_db
def test_business_knowledge_faqs_reach_the_prompt(account):
    BusinessKnowledge.objects.create(
        account=account,
        faqs=[
            {"q": "Does TaskCentro charge freelancers?", "a": "No, joining is free."}
        ],
        common_questions=[{"q": "", "a": "skipped: no question"}],
    )
    facts = business_facts.build(account)
    assert facts["knowledge"] == [
        {
            "id": "f0",
            "title": "Does TaskCentro charge freelancers?",
            "content": "No, joining is free.",
        }
    ]


@pytest.mark.django_db
def test_the_question_asked_is_sent_to_the_model_even_far_down_the_list(
    account, join_entry, sent
):
    from apps.ai import facts as f

    # Push the knowledge base over budget so selection, not "everything", decides.
    KnowledgeBaseEntry.objects.filter(account=account).exclude(pk=join_entry.pk).update(
        content="x" * (f.MAX_KNOWLEDGE_CHARS // 30)
    )
    Fake.answer = answer(sources=[f"k{join_entry.pk}"])
    draft(convo(account))
    assert f"[k{join_entry.pk}] Q: Zz {JOIN_Q}" in Fake.systems[-1]


# ---- the contract -----------------------------------------------------------------------------


def test_sources_keep_only_ids_that_were_offered():
    p = proposals.parse(
        answer(sources=["k1", "k99", "[k1]", "f0"]),
        templates={},
        window_open=True,
        knowledge_ids=["k1", "f0"],
    )
    assert p["intent"] == "faq"
    assert p["sources"] == ["k1", "f0"] and p["payload"]["sources"] == ["k1", "f0"]


def test_sensitive_is_an_intent():
    p = proposals.parse(answer(intent="sensitive"), templates={}, window_open=True)
    assert p["intent"] == "sensitive"


# ---- answering on its own ---------------------------------------------------------------------


@pytest.mark.django_db
def test_taskcentro_a_question_the_business_answered_is_answered_on_its_own(
    account, join_entry, sent
):
    Fake.answer = answer(sources=[f"k{join_entry.pk}"])
    c = convo(account)
    p, result = draft(c)
    assert result == "sent" and p.auto_sent_at is not None
    assert sent == [
        {"conversation": c, "body": JOIN_A, "idempotency_key": f"ai-auto:{p.pk}"}
    ]
    assert p.intent == "faq" and p.route == "standard"
    grounded = next(c for c in p.auto_decision["checks"] if c["name"] == "grounded")
    assert grounded["ok"] and grounded["value"] == [f"k{join_entry.pk}"]
    # The only "Akilent" the model sees is the rule never to speak as it.
    assert Fake.systems[-1].count("Akilent") == 1
    assert "on behalf of TaskCentro" in Fake.systems[-1]


@pytest.mark.django_db
def test_on_instagram_too(account, join_entry, sent, monkeypatch):
    monkeypatch.setattr(
        "apps.conversations.api.conversation_window_is_open", lambda c: True
    )
    Fake.answer = answer(sources=[f"k{join_entry.pk}"])
    c = convo(account, channel="instagram")
    p, result = draft(c)
    assert result == "sent" and sent[0]["conversation"] == c
    assert "on behalf of TaskCentro on Instagram" in Fake.systems[-1]
    assert "WhatsApp" not in Fake.systems[-1]


@pytest.mark.django_db
def test_an_faq_answer_that_cites_nothing_is_not_sent(account, join_entry, sent):
    Fake.answer = answer(sources=[])
    p, result = draft(convo(account))
    assert result == "ready" and p.auto_sent_at is None
    check = next(c for c in p.auto_decision["checks"] if c["name"] == "grounded")
    assert not check["ok"] and "knowledge base backs" in check["detail"]
    assert [kw["body"] for kw in sent] == [DEFAULT_HOLDING_REPLY]


@pytest.mark.django_db
def test_faq_is_locked_until_the_business_writes_some_answers(account, sent):
    from apps.ai import autonomy

    assert "faq" in autonomy.locked_topics(account)
    KnowledgeBaseEntry.objects.create(account=account, title="Q", content="A")
    assert "faq" not in autonomy.locked_topics(account)


# ---- sensitive topics and the holding reply ---------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body,intent",
    [
        ("I want a refund, the freelancer never showed up", "faq"),
        ("How do I join?", "sensitive"),
    ],
)
def test_sensitive_messages_get_a_person(account, join_entry, sent, body, intent):
    Fake.answer = answer(intent=intent, sources=[f"k{join_entry.pk}"])
    p, result = draft(convo(account), body=body)
    assert result == "ready" and p.auto_sent_at is None
    assert [kw["body"] for kw in sent] == [DEFAULT_HOLDING_REPLY]
    check = next(c for c in p.auto_decision["checks"] if c["name"] == "not_sensitive")
    assert not check["ok"] and "complaint, refund" in check["detail"]


@pytest.mark.django_db
def test_one_holding_reply_per_wait_for_the_team(account, sent):
    Fake.answer = answer(action="handoff", intent="other")
    c = convo(account)
    first, _ = draft(c, body="Can I get a custom quote?")
    second, _ = draft(c, body="Hello?")
    assert first.auto_decision["holding_sent"] is True
    assert "holding_sent" not in second.auto_decision
    assert len(sent) == 1 and sent[0]["idempotency_key"] == f"ai-auto:hold:{first.pk}"

    # Once the team has replied (and gone quiet), a new wait can get a new holding reply.
    AIProposal.objects.filter(pk__in=[first.pk, second.pk]).update(
        created_at=timezone.now() - timedelta(hours=1)
    )
    team_reply(c, minutes_ago=30)
    third, _ = draft(c, body="One more question")
    assert third.auto_decision["holding_sent"] is True and len(sent) == 2


@pytest.mark.django_db
def test_no_holding_reply_when_a_teammate_has_the_conversation(account, sent):
    from django.contrib.auth.models import User

    Fake.answer = answer(action="handoff", intent="other")
    c = convo(account)
    c.assigned_to = User.objects.create_user("t", "t@example.com", "pw")
    c.save()
    p, _ = draft(c)
    assert sent == [] and "holding_sent" not in p.auto_decision


@pytest.mark.django_db
def test_holding_reply_can_be_switched_off_or_reworded(account, sent):
    Fake.answer = answer(action="handoff", intent="other")
    AISettings.objects.filter(account=account).update(holding_reply_enabled=False)
    draft(convo(account))
    assert sent == []

    AISettings.objects.filter(account=account).update(
        holding_reply_enabled=True,
        holding_reply_text="Thanks! A TaskCentro agent will reply.",
    )
    draft(convo(account, phone="+260971000002"))
    assert [kw["body"] for kw in sent] == ["Thanks! A TaskCentro agent will reply."]


@pytest.mark.django_db
def test_the_inbox_says_why_and_that_the_customer_was_told(account, sent):
    from apps.ai import api as ai_api

    Fake.answer = answer(action="handoff", intent="other")
    p, _ = draft(convo(account))
    note = ai_api.serialize(p, p.conversation)["autoNote"]
    assert note.startswith("Not sent automatically:")
    assert note.endswith("AI told the customer someone from your team will reply.")

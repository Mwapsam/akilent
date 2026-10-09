"""A reply that cites offered Q&A entries is a knowledge-base answer even when the model labels it
"other": ``proposals.validate`` relabels it "faq", so the topic gate, the grounded check and the
stored intent all agree. Uncited or made-up citations stay "other" and are held."""

import pytest

from apps.accounts.models import Account
from apps.ai import facts as business_facts
from apps.ai import proposals
from apps.ai.autonomy import preview
from apps.ai.models import AISettings, KnowledgeBaseEntry


@pytest.fixture
def account(db):
    acc = Account.objects.create(company_name="TaskCentro")
    AISettings.objects.create(
        account=acc,
        enabled=True,
        reply_mode="auto",
        auto_topics=["faq", "greeting", "hours"],
        auto_min_confidence=0.75,
    )
    return acc


@pytest.fixture
def entry(account):
    return KnowledgeBaseEntry.objects.create(
        account=account,
        title="How do I sign up as a professional?",
        content="Click Sign Up as a Professional, fill in your details and we'll verify you.",
    )


def _validated(facts, *, intent, sources, action="reply"):
    data = {
        "version": 1,
        "action": action,
        "intent": intent,
        "confidence": 0.85,
        "sources": sources,
        "payload": {"text": "Sign up as a professional and complete verification."}
        if action == "reply"
        else {"note": "Needs a person."},
    }
    return proposals.validate(
        data,
        templates={},
        window_open=True,
        knowledge_ids=[e["id"] for e in facts["knowledge"]],
    )


def _checks(account, proposal, facts):
    decision = preview(
        ai_settings=AISettings.objects.get(account=account),
        proposal=proposal,
        account=account,
        facts=facts,
        customer_text="How do I create my freelancer profile?",
    )
    return decision, {c["name"]: c for c in decision.checks}


@pytest.mark.django_db
class TestCitedOtherBecomesFaq:
    def test_freelancer_profile_answer_citing_kb_is_sent(self, account, entry):
        facts = business_facts.build(account)
        proposal = _validated(facts, intent="other", sources=[f"k{entry.pk}"])
        assert proposal["intent"] == "faq"
        decision, checks = _checks(account, proposal, facts)
        assert checks["allowed_topic"]["ok"]
        assert checks["grounded"]["ok"]
        assert decision.send

    def test_other_without_sources_is_held(self, account, entry):
        facts = business_facts.build(account)
        proposal = _validated(facts, intent="other", sources=[])
        assert proposal["intent"] == "other"
        decision, checks = _checks(account, proposal, facts)
        assert not checks["allowed_topic"]["ok"]
        assert not decision.send

    def test_other_citing_unknown_id_is_held(self, account, entry):
        facts = business_facts.build(account)
        proposal = _validated(facts, intent="other", sources=["k9999"])
        assert proposal["intent"] == "other"
        assert proposal["sources"] == []
        decision, _checks_by_name = _checks(account, proposal, facts)
        assert not decision.send

    def test_faq_citing_unknown_id_fails_grounded(self, account, entry):
        facts = business_facts.build(account)
        proposal = _validated(facts, intent="faq", sources=["k9999"])
        decision, checks = _checks(account, proposal, facts)
        assert not checks["grounded"]["ok"]
        assert not decision.send

    def test_handoff_keeps_its_intent(self, account, entry):
        facts = business_facts.build(account)
        proposal = _validated(
            facts, intent="other", sources=[f"k{entry.pk}"], action="handoff"
        )
        assert proposal["intent"] == "other"

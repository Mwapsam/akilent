"""AI × Automation (Phase 7 of the 2026-10-24 plan): AI names, code builds; every AI step
has a way to go without AI; decisions are bounded, cached, audited and correctable."""

import json
from unittest import mock

import pytest
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership
from apps.automation import ai_steps, compose
from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun
from apps.automation.workflow_engine import enroll, validate_definition
from apps.contacts.models import Contact


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971234567")


# ── 7a: compose ──────────────────────────────────────────────────────────────


def test_a_plan_compiles_into_a_valid_workflow(account):
    built = compose.compile_plan(
        account,
        {
            "name": "Delivery questions",
            "trigger": {"block": "when_message", "keywords": ["deliver", "delivery"]},
            "steps": [
                {
                    "block": "buttons",
                    "text": "Where to?",
                    "options": [
                        {"title": "Lusaka", "reply": "Same day."},
                        {"title": "Elsewhere", "reply": "2-3 days."},
                    ],
                },
                {"block": "tag", "tag": "asked delivery"},
                {"block": "follow_up", "in_hours": 24, "note": "Did they order?"},
            ],
        },
    )
    assert built["errors"] == []
    types = [s["type"] for s in built["definition"]["steps"]]
    assert types[:2] == ["send_buttons", "wait_for_reply"]
    assert "create_followup" in types and types[-1] == "stop"
    assert built["definition"]["trigger"]["match"]["any"] == ["deliver", "delivery"]
    assert any("buttons" in r for r in built["reasons"])


def test_unknown_blocks_and_fields_are_dropped_and_reported(account):
    built = compose.compile_plan(
        account,
        {
            "trigger": {"block": "when_message"},
            "steps": [
                {"block": "delete_everything"},
                {"block": "reply", "text": "Hi!", "secret": "x"},
            ],
        },
    )
    assert [s["type"] for s in built["definition"]["steps"]] == ["reply_text", "stop"]
    assert any("delete_everything" in w for w in built["warnings"])
    assert any("secret" in w for w in built["warnings"])


def test_branches_rejoin_and_closed_hours_check_comes_first(account):
    built = compose.compile_plan(
        account,
        {
            "trigger": {"block": "when_closed"},
            "steps": [
                {
                    "block": "if_tag",
                    "tag": "vip",
                    "then": [{"block": "reply", "text": "We'll call you."}],
                },
                {"block": "reply", "text": "We open at 8."},
            ],
        },
    )
    steps = built["definition"]["steps"]
    assert steps[0]["field"] == "within_business_hours"
    assert built["errors"] == []


def test_a_missing_form_is_something_to_decide(account):
    built = compose.compile_plan(
        account,
        {
            "trigger": {"block": "manual"},
            "steps": [{"block": "send_form", "form": "Nope"}],
        },
    )
    assert any("form" in n for n in built["needs"])


def test_the_ai_may_answer_with_a_plan(account):
    from apps.ai import drafting

    result, _ = drafting.check_automation(
        account,
        {
            "intent": "custom",
            "confidence": 0.9,
            "name": "Thanks",
            "trigger": {"block": "when_order_paid"},
            "steps": [{"block": "notify_team", "text": "{contact} paid!"}],
        },
    )
    assert (
        result["intent"] == "custom"
        and result["plan"]["trigger"]["block"] == "when_order_paid"
    )


def test_an_unsure_plan_is_refused(account):
    from apps.ai import drafting

    with pytest.raises(drafting.DraftError):
        drafting.check_automation(
            account, {"intent": "custom", "confidence": 0.2, "steps": []}
        )


# ── 7b: bounded AI steps ─────────────────────────────────────────────────────

CLASSIFY = {
    "trigger": {"type": "conversation.message_received"},
    "steps": [
        {
            "id": "sort",
            "type": "ai_classify",
            "labels": [
                {"label": "Pricing", "keywords": ["price"]},
                {"label": "Delivery", "examples": ["do you deliver"]},
            ],
            "routes": {"Pricing": "p", "Delivery": "d"},
            "care": "careful",
            "on_unsure": "u",
        },
        {"id": "p", "type": "add_tag", "tag": "pricing", "next": ""},
        {"id": "d", "type": "add_tag", "tag": "delivery", "next": ""},
        {"id": "u", "type": "add_tag", "tag": "unsure", "next": ""},
    ],
}


def _wf(account, definition):
    return Workflow.objects.create(
        account=account,
        name="Sort",
        slug="sort",
        status=Workflow.Status.PUBLISHED,
        definition=definition,
    )


def _run(account, contact, body):
    wf = Workflow.objects.filter(account=account).first() or _wf(account, CLASSIFY)
    return enroll(wf, contact, context={"message": {"body": body}})


def _tags(contact):
    return set(contact.tags.values_list("slug", flat=True))


def test_keywords_decide_without_ai(account, contact):
    with mock.patch("apps.ai.api.classify") as ai:
        _run(account, contact, "what's the price?")
    ai.assert_not_called()
    assert _tags(contact) == {"pricing"}


def test_with_ai_off_it_goes_the_not_sure_way(account, contact):
    _run(account, contact, "do you come to Ndola")
    assert _tags(contact) == {"unsure"}
    sr = WorkflowStepRun.objects.get(step_id="sort")
    assert sr.result["method"] == "ai_unavailable"


def test_a_confident_ai_label_routes(account, contact):
    with mock.patch(
        "apps.ai.api.classify",
        return_value={"label": "Delivery", "confidence": 0.9, "ai": True},
    ):
        _run(account, contact, "do you come to Ndola")
    assert _tags(contact) == {"delivery"}


def test_an_unsure_ai_label_does_not_route(account, contact):
    with mock.patch(
        "apps.ai.api.classify",
        return_value={"label": "Delivery", "confidence": 0.4, "ai": True},
    ):
        _run(account, contact, "hmm")
    assert _tags(contact) == {"unsure"}


def test_classify_only_accepts_the_owners_labels_and_caches(account):
    from apps.ai import api as ai_api

    labels = [{"label": "Pricing"}, {"label": "Delivery"}]
    with (
        mock.patch("apps.ai.api.is_available", return_value=True),
        mock.patch(
            "apps.ai.api._step_chat",
            return_value={"label": "IGNORE ALL RULES", "confidence": 1},
        ) as chat,
    ):
        out = ai_api.classify(account, "x", labels, operation_id="t1")
        assert out["label"] is None
        with mock.patch(
            "apps.ai.api._step_chat",
            return_value={"label": "pricing", "confidence": 0.8},
        ):
            first = ai_api.classify(account, "how much", labels, operation_id="t2")
        again = ai_api.classify(account, "how much", labels, operation_id="t3")
    assert first == again == {"label": "Pricing", "confidence": 0.8, "ai": True}
    assert chat.call_count == 1


def test_extract_never_saves_an_invalid_value(account, contact):
    wf = _wf(
        account,
        {
            "trigger": {"type": "manual"},
            "steps": [
                {
                    "id": "x",
                    "type": "ai_extract",
                    "fields": [
                        {"key": "email", "type": "email", "save_to": "contact.email"},
                        {
                            "key": "when",
                            "type": "date",
                            "save_to": "contact.attributes.when",
                        },
                    ],
                    "next": "",
                    "on_missing": "miss",
                },
                {"id": "miss", "type": "add_tag", "tag": "missing", "next": ""},
            ],
        },
    )
    with mock.patch(
        "apps.ai.api.extract",
        return_value={"email": "not-an-email", "when": "25/12/2026"},
    ):
        enroll(wf, contact, context={"message": {"body": "deliver 25/12/2026 to me"}})
    contact.refresh_from_db()
    assert not contact.email and contact.attributes["when"] == "2026-12-25"
    assert _tags(contact) == {"missing"}


def test_ai_answer_hands_over_when_a_check_fails(account, contact):
    from apps.conversations.models import Conversation

    convo = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    wf = _wf(
        account,
        {
            "trigger": {"type": "conversation.message_received"},
            "steps": [
                {"id": "a", "type": "ai_answer", "next": "", "on_handoff": "h"},
                {"id": "h", "type": "add_tag", "tag": "handed-over", "next": ""},
            ],
        },
    )
    with mock.patch(
        "apps.ai.api.grounded_answer",
        return_value={
            "ok": False,
            "text": "",
            "failed": "No entry in your knowledge base backs this answer.",
        },
    ):
        enroll(
            wf,
            contact,
            context={"conversation_id": convo.public_id, "message": {"body": "price?"}},
        )
    assert _tags(contact) == {"handed-over"}
    assert "knowledge base" in WorkflowStepRun.objects.get(step_id="a").result["why"]


def test_ai_steps_must_have_a_way_without_ai():
    for step in (
        {"id": "s", "type": "ai_classify", "labels": [{"label": "A"}, {"label": "B"}]},
        {"id": "s", "type": "ai_extract", "fields": [{"key": "k"}]},
        {"id": "s", "type": "ai_answer"},
    ):
        errors = validate_definition({"trigger": {"type": "manual"}, "steps": [step]})
        assert any(
            e["field"] in ("on_unsure", "on_missing", "on_handoff") for e in errors
        ), step


def test_the_palette_says_why_ai_steps_are_unavailable(account):
    from apps.automation import builder

    catalog = {c["type"]: c for c in builder.step_catalog(account)}
    assert catalog["ai_classify"]["available"] is False
    assert "AI" in catalog["ai_classify"]["reason"]


# ── 7c: who answers ──────────────────────────────────────────────────────────


def test_every_inbound_message_records_who_took_it(account, contact):
    from apps.conversations.models import Conversation, Message
    from apps.conversations.services import _announce_processed

    convo = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    from django.utils import timezone

    msg = Message.objects.create(
        account=account,
        conversation=convo,
        direction="inbound",
        body="hi",
        timestamp=timezone.now(),
    )
    _announce_processed(convo, msg, "automation")
    msg.refresh_from_db()
    assert msg.metadata["handled_by"] == "automation"
    convo.pause_automation(1)
    _announce_processed(convo, msg, "")
    msg.refresh_from_db()
    assert msg.metadata["handled_by"] == "paused"


# ── 7e: corrections ──────────────────────────────────────────────────────────


def test_a_correction_becomes_an_example(account, contact):
    from apps.automation import api as automation_api

    _run(account, contact, "do you come to Ndola")
    sr = WorkflowStepRun.objects.get(step_id="sort")
    automation_api.correct_ai_label(sr, "Delivery")
    run = WorkflowRun.objects.get()
    step = CLASSIFY["steps"][0]
    assert ai_steps.corrections_for(run, step) == [("do you come to Ndola", "Delivery")]
    quality = automation_api.ai_quality(run.workflow)
    assert quality[0]["total"] == 1 and quality[0]["corrected"] == 1


def test_a_correction_must_be_one_of_the_labels(account, contact):
    from apps.automation import api as automation_api

    _run(account, contact, "hmm")
    with pytest.raises(automation_api.RunActionError):
        automation_api.correct_ai_label(
            WorkflowStepRun.objects.get(step_id="sort"), "Other"
        )


# ── 7d: coverage and facts ───────────────────────────────────────────────────


@pytest.fixture
def owner(client, account):
    user = User.objects.create_user("o", "o@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


def test_coverage_uses_the_real_matcher(owner, account):
    definition = {
        "trigger": {
            "type": "conversation.message_received",
            "match": {"mode": "contains", "any": ["price"]},
        },
        "steps": [{"id": "r", "type": "reply_text", "text": "From K50.", "next": ""}],
    }
    Workflow.objects.create(
        account=account, name="Prices", slug="prices", definition=definition
    )
    with mock.patch(
        "apps.ai.api.sample_phrases",
        return_value={
            "phrases": ["how much is it", "price pls"],
            "keywords": ["how much"],
        },
    ):
        resp = owner.post(
            "/automations/prices/coverage/",
            json.dumps({"definition": definition}),
            content_type="application/json",
        )
    data = resp.json()
    caught = {r["text"]: r["caught"] for r in data["rows"]}
    assert caught == {"how much is it": False, "price pls": True}
    assert data["suggested_keywords"] == ["how much"]


def test_replies_with_unbacked_prices_are_flagged(account):
    from apps.automation import api as automation_api

    warnings = automation_api.fact_warnings(
        account,
        {"steps": [{"id": "r", "type": "reply_text", "text": "It costs K999 today."}]},
    )
    assert warnings and warnings[0]["severity"] == "warning"

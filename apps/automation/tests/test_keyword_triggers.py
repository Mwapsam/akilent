"""Keyword-triggered auto-replies: deterministic, no AI.

A workflow on "a customer messages you" can carry ``trigger.match`` (any-of keywords) and
answers with a ``reply_text`` step. These pin the parts an owner relies on: it fires on the
right messages, not the wrong ones, never spams a repeat question, and cannot be saved in a
shape that would silently do nothing.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.automation import keywords
from apps.automation.models import AutomationRule, Workflow, WorkflowRun
from apps.automation.rules import evaluate_conditions
from apps.automation.workflow_engine import enroll_for_trigger, validate_definition
from apps.contacts.models import Contact
from apps.conversations.models import Conversation
from apps.whatsapp.models import OutboundMessage, WhatsAppContact
from apps.whatsapp.models import Conversation as WhatsAppConversation

TRIGGER = "conversation.message_received"


# --- the matcher ---------------------------------------------------------------


@pytest.mark.parametrize("mode, words, body, expected", [
    ("contains", ["price"], "What is the PRICE?", True),
    ("contains", ["price", "cost"], "how much does it cost", True),
    ("contains", ["hi"], "this is nice", False),               # whole words only
    ("contains", ["hi"], "Hi!", True),
    ("contains", ["opening hours"], "what are your opening   hours today", True),
    ("contains", ["opening hours"], "hours of opening", False),  # phrase order matters
    ("contains", ["price"], "prices please", False),            # no guessing at plurals
    ("starts_with", ["hello", "hi"], "Hello, do you deliver?", True),
    ("starts_with", ["hello"], "oh hello", False),
    ("exact", ["menu"], " Menu! ", True),
    ("exact", ["menu"], "the menu please", False),
    ("contains", ["price"], "", False),
    ("contains", [""], "anything", False),
])
def test_matcher_modes(mode, words, body, expected):
    assert keywords.matches({"mode": mode, "any": words}, body) is expected


def test_no_rule_matches_everything():
    assert keywords.matches(None, "anything") and keywords.matches({}, "anything")


# --- validation ----------------------------------------------------------------


def _definition(trigger=None, steps=None):
    return {
        "trigger": trigger or {"type": TRIGGER, "match": {"mode": "contains", "any": ["price"]}},
        "steps": steps or [
            {"id": "reply", "type": "reply_text", "text": "Prices start at K50.", "next": "stop"},
            {"id": "stop", "type": "stop"},
        ],
    }


def _fields(definition):
    return {e["field"] for e in validate_definition(definition) if e["severity"] == "error"}


def test_a_keyword_reply_workflow_is_valid():
    assert not validate_definition(_definition())


@pytest.mark.parametrize("match", [
    {"mode": "contains", "any": []},
    {"mode": "contains"},
    {"mode": "sounds_like", "any": ["price"]},
    {"mode": "contains", "any": [42]},
    {"mode": "contains", "any": ["x" * 200]},
    "price",
])
def test_a_bad_match_is_refused(match):
    definition = _definition(trigger={"type": TRIGGER, "match": match})
    assert "trigger.match" in _fields(definition)


def test_match_is_refused_on_a_trigger_that_carries_no_message():
    definition = _definition(trigger={"type": "contact.created", "match": {"any": ["price"]}})
    assert "trigger.match" in _fields(definition)


def test_reply_text_needs_text_and_a_message_trigger():
    assert "text" in _fields(_definition(steps=[
        {"id": "reply", "type": "reply_text", "text": "  ", "next": "stop"}, {"id": "stop", "type": "stop"}]))
    assert "type" in _fields(_definition(trigger={"type": "contact.created"}))


def test_a_negative_cooldown_is_refused():
    definition = _definition(trigger={"type": TRIGGER, "cooldown_minutes": -5})
    assert "trigger.cooldown_minutes" in _fields(definition)


# --- behaviour through the real engine -------------------------------------------


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def customer(account):
    contact = Contact.objects.create(account=account, phone="+260971234567", first_name="Ada", source="whatsapp")
    wa = WhatsAppContact.objects.create(account=account, phone_number="+260971234567", contact=contact)
    wa_conversation = WhatsAppConversation.get_or_open(wa)
    conversation = Conversation.get_or_create_for_whatsapp(wa_conversation)
    return contact, conversation


def publish(account, definition, slug="price-answer"):
    from apps.automation import api as automation_api

    return automation_api.upsert_published_workflow(account, slug=slug, name=slug, definition=definition)


def message(conversation, body):
    return {"conversation_id": conversation.public_id, "message": {"body": body, "type": "text"}}


def enrol(account, contact, conversation, body):
    return enroll_for_trigger(account.id, TRIGGER, contact, context=message(conversation, body))


@pytest.mark.django_db
def test_a_matching_message_sends_the_reply_with_the_customers_name(account, customer):
    contact, conversation = customer
    publish(account, _definition(steps=[
        {"id": "reply", "type": "reply_text", "text": "Hi {first_name}, prices start at K50.", "next": "stop"},
        {"id": "stop", "type": "stop"}]))

    assert enrol(account, contact, conversation, "How much is the price?") == 1

    sent = OutboundMessage.objects.get(account=account)
    assert sent.payload["body"] == "Hi Ada, prices start at K50."
    assert WorkflowRun.objects.get().status == WorkflowRun.Status.COMPLETED


@pytest.mark.django_db
def test_a_message_that_does_not_match_starts_nothing(account, customer):
    contact, conversation = customer
    publish(account, _definition())
    assert enrol(account, contact, conversation, "Are you open on Sunday?") == 0
    assert not WorkflowRun.objects.exists() and not OutboundMessage.objects.exists()


@pytest.mark.django_db
def test_a_workflow_without_match_still_answers_every_message(account, customer):
    """Existing workflows must not change behaviour."""
    contact, conversation = customer
    publish(account, _definition(trigger={"type": TRIGGER}))
    assert enrol(account, contact, conversation, "anything at all") == 1


@pytest.mark.django_db
def test_a_repeated_question_is_answered_once_within_the_cooldown(account, customer):
    contact, conversation = customer
    publish(account, _definition())
    assert enrol(account, contact, conversation, "price?") == 1
    assert enrol(account, contact, conversation, "price??") == 0
    assert OutboundMessage.objects.count() == 1


@pytest.mark.django_db
def test_the_question_is_answered_again_after_the_cooldown(account, customer):
    contact, conversation = customer
    workflow = publish(account, _definition())
    enrol(account, contact, conversation, "price?")
    WorkflowRun.objects.filter(workflow=workflow).update(started_at=timezone.now() - timedelta(minutes=61))
    assert enrol(account, contact, conversation, "price?") == 1


@pytest.mark.django_db
def test_the_cooldown_is_configurable_and_can_be_switched_off(account, customer):
    contact, conversation = customer
    publish(account, _definition(trigger={
        "type": TRIGGER, "match": {"any": ["price"]}, "cooldown_minutes": 0}))
    assert enrol(account, contact, conversation, "price") == 1
    assert enrol(account, contact, conversation, "price") == 1


@pytest.mark.django_db
def test_two_keyword_workflows_answer_their_own_questions(account, customer):
    contact, conversation = customer
    publish(account, _definition(), slug="price")
    publish(account, _definition(
        trigger={"type": TRIGGER, "match": {"any": ["where", "location"]}},
        steps=[{"id": "reply", "type": "reply_text", "text": "We are on Cairo Road.", "next": "stop"},
               {"id": "stop", "type": "stop"}]), slug="where")
    enrol(account, contact, conversation, "Where are you?")
    assert [m.payload["body"] for m in OutboundMessage.objects.all()] == ["We are on Cairo Road."]


@pytest.mark.django_db
def test_a_reply_step_without_a_conversation_fails_loudly_not_silently(account, customer):
    contact, _ = customer
    publish(account, _definition(trigger={"type": TRIGGER}))
    enroll_for_trigger(account.id, TRIGGER, contact, context={"message": {"body": "price"}})
    run = WorkflowRun.objects.get()
    assert run.status == WorkflowRun.Status.FAILED
    assert not OutboundMessage.objects.exists()


@pytest.mark.django_db
def test_a_workflow_cannot_reply_in_another_accounts_conversation(account, customer):
    contact, conversation = customer
    other = Account.objects.create(company_name="Other Co")
    other_contact = Contact.objects.create(account=other, phone="+260979999999")
    publish(other, _definition(trigger={"type": TRIGGER}), slug="theirs")
    enroll_for_trigger(other.id, TRIGGER, other_contact, context=message(conversation, "price"))
    assert WorkflowRun.objects.get().status == WorkflowRun.Status.FAILED
    assert not OutboundMessage.objects.exists()


# --- the legacy rule path ------------------------------------------------------------


@pytest.mark.django_db
def test_legacy_rule_string_conditions_match_on_substring(account):
    """A matching substring used to fall through to an equality check and fail."""
    rule = AutomationRule(account=account, conditions={"message_contains": "price"})
    assert evaluate_conditions(rule, {"message_contains": "What is the price of rice?"})
    assert not evaluate_conditions(rule, {"message_contains": "Are you open?"})
    assert evaluate_conditions(AutomationRule(account=account, conditions={"n": 3}), {"n": 3})
    assert not evaluate_conditions(AutomationRule(account=account, conditions={"n": 3}), {"n": 4})


# --- the editor ------------------------------------------------------------------------


@pytest.fixture
def owner_client(client, account):
    from django.contrib.auth.models import User

    from apps.accounts.models import Membership

    user = User.objects.create_user("owner", "owner@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_the_editor_offers_the_message_trigger_keywords_and_reply_step(owner_client, account):
    workflow = Workflow.objects.create(
        account=account, name="Price", slug="price", definition=_definition())
    body = owner_client.get(f"/automations/{workflow.slug}/").content.decode()
    assert "conversation.message_received" in body       # selectable trigger
    assert "keywordDraft" in body and "reply_text" in body
    assert "price" in body                                # the saved keyword is loaded


@pytest.mark.django_db
def test_a_keyword_workflow_saves_and_publishes_from_the_editor(owner_client, account):
    import json

    workflow = Workflow.objects.create(
        account=account, name="Price", slug="price",
        definition={"trigger": {"type": "manual"}, "steps": [{"id": "stop", "type": "stop"}]})
    resp = owner_client.post(
        f"/automations/{workflow.slug}/save/", data=json.dumps({"definition": _definition()}),
        content_type="application/json")
    assert resp.status_code == 200 and resp.json()["errors"] == []
    owner_client.post(f"/automations/{workflow.slug}/publish/")
    workflow.refresh_from_db()
    assert workflow.status == Workflow.Status.PUBLISHED
    assert workflow.definition["trigger"]["match"]["any"] == ["price"]

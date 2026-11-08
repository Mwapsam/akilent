"""One form status everywhere, readable publish errors, and sending while changes wait."""

from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.conversations import flow_json
from apps.conversations import forms as conversation_forms
from apps.conversations.flow_errors import GENERIC, humanize_flow_error
from apps.conversations.models import ConversationForm

QUESTIONS = [
    {"key": "name", "label": "Name", "field_type": "text"},
    {"key": "size", "label": "Size", "field_type": "choice", "options": ["S", "M"]},
]


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Biz")


def _form(account, **kw):
    kw.setdefault("status", ConversationForm.Status.PUBLISHED)
    kw.setdefault("presentation", ConversationForm.Presentation.WHATSAPP_FLOW)
    return ConversationForm.objects.create(
        account=account, name="Register", questions=QUESTIONS, **kw
    )


# ── display_status ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fields, key, label",
    [
        ({"status": "draft"}, "draft", "Draft"),
        ({"status": "archived"}, "paused", "Archived"),
        ({"presentation": "text"}, "live", "Live"),
        ({}, "pending", "Not on WhatsApp yet"),
        ({"flow_status": "publishing"}, "pending", "Publishing…"),
        ({"flow_status": "error", "flow_error": "Bad"}, "error", "Not published"),
        ({"flow_status": "published", "flow_id": "F", "live": True}, "live", "Live"),
        (
            {"flow_status": "needs_republish", "flow_id": "F", "live": True},
            "pending",
            "Changes not published",
        ),
        (
            {"flow_status": "publishing", "flow_id": "F", "live": True},
            "pending",
            "Publishing…",
        ),
        (
            {"flow_status": "error", "flow_id": "F", "live": True},
            "error",
            "Changes not published",
        ),
    ],
)
def test_one_status_for_every_state(account, fields, key, label):
    fields = dict(fields)
    if fields.pop("live", False):
        fields["flow_published_at"] = timezone.now()
    status = _form(account, **fields).display_status
    assert (status["key"], status["label"]) == (key, label)


def test_the_register_form_case_no_longer_says_published_and_not_published(account):
    """Ready to send, but Meta rejected the Flow: one status, Not published, with why."""
    form = _form(
        account, flow_status="error", flow_error="Question 1 needs a field name."
    )
    status = form.display_status
    assert status["label"] == "Not published"
    assert status["reason"] == "Question 1 needs a field name."


def test_a_live_form_with_waiting_changes_says_so(account):
    form = _form(
        account,
        flow_status="needs_republish",
        flow_id="F",
        flow_published_at=timezone.now(),
    )
    assert (
        "Publish your changes before sending it again" in form.display_status["reason"]
    )


# ── Live means sendable ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "flow_status",
    ["not_created", "draft", "published", "needs_republish", "publishing", "error"],
)
@pytest.mark.parametrize("was_published", [True, False])
def test_live_exactly_when_it_can_be_sent(account, flow_status, was_published):
    """Answers are matched against the current questions, so a form with unpublished
    changes can't be sent, and mustn't be shown as Live."""
    form = _form(
        account,
        flow_status=flow_status,
        flow_id="F",
        flow_published_at=timezone.now() if was_published else None,
    )
    conversation = SimpleNamespace(
        channel="whatsapp", Channel=SimpleNamespace(WHATSAPP="whatsapp")
    )
    assert (form.display_status["key"] == "live") == conversation_forms.can_start(
        form, conversation
    )


def test_a_never_published_flow_cant_be_sent(account):
    form = _form(account, flow_status="error", flow_id="DRAFT")
    conversation = SimpleNamespace(
        channel="whatsapp", Channel=SimpleNamespace(WHATSAPP="whatsapp")
    )
    assert not conversation_forms.can_start(form, conversation)


# ── Readable errors ─────────────────────────────────────────────────────────


def test_the_raw_meta_100_becomes_words():
    text = humanize_flow_error(
        code="100", message="Meta API 400 [100]: (#100) The parameter name is required."
    )
    assert "(#100)" not in text and "problem on our side" in text


@pytest.mark.parametrize(
    "code, words",
    [
        ("190", "Reconnect WhatsApp"),
        ("200", "allow every permission"),
        ("130429", "busy"),
        ("139004", "blocked"),
        ("100/33", "no longer exists"),
        ("", GENERIC),
        ("999999", GENERIC),
    ],
)
def test_codes_map_to_actions(code, words):
    assert words in humanize_flow_error(code=code, message="raw")


def test_a_missing_name_pointer_names_the_question():
    built = flow_json.build(QUESTIONS, title="T")
    built["screens"][0]["layout"]["children"][0]["children"][1]["name"] = ""
    problem = {
        "message": "Required property 'name' is missing",
        "pointers": [{"path": "screens[0].layout.children[0].children[1].name"}],
    }
    text = humanize_flow_error(
        code="FLOW_VALIDATION", problems=[problem], flow_json=built, questions=QUESTIONS
    )
    assert text == "Question 2 needs a field name."


def test_an_intro_shifts_the_component_index():
    built = flow_json.build(QUESTIONS, title="T", intro="Hello")
    problem = {
        "message": "x",
        "pointers": [{"path": "screens[0].layout.children[0].children[1].label"}],
    }
    text = humanize_flow_error(
        code="FLOW_VALIDATION", problems=[problem], flow_json=built, questions=QUESTIONS
    )
    assert text.startswith("Question 1:")


def test_footer_and_unplaceable_pointers():
    built = flow_json.build(QUESTIONS, title="T")
    footer = {
        "message": "Too long",
        "pointers": [{"path": "screens[0].layout.children[0].children[2].label"}],
    }
    odd = {"message": "Something odd", "pointers": [{"path": "nowhere[9]"}]}
    text = humanize_flow_error(
        code="FLOW_VALIDATION",
        problems=[footer, odd],
        flow_json=built,
        questions=QUESTIONS,
    )
    assert "The button text wasn't accepted by WhatsApp: Too long." in text
    assert "WhatsApp didn't accept the form: Something odd." in text


def test_humanize_never_raises_on_junk():
    assert humanize_flow_error(
        code="FLOW_VALIDATION", problems=[None, 3, {"pointers": "x"}]
    )


# ── Checks before publishing ────────────────────────────────────────────────


def test_a_key_meta_would_reject_is_caught_before_publishing():
    problems = flow_json.check(
        [{"key": "first-name", "label": "Name", "field_type": "text"}]
    )
    assert problems == [
        "Question 1: its field name “first-name” can only use letters, numbers and underscores."
    ]

"""Engine safety (Phase 1 of the 2026-10-24 automation plan).

Runs follow the version they started on; pause holds and resumes fairly; archive lets
everyone go; opted-out customers and STOP messages start nothing; a closed messaging window
fails early (or uses a fallback template); one message gets one answer.
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation import api as automation_api
from apps.automation.models import Workflow, WorkflowInteraction, WorkflowRun
from apps.automation.workflow_engine import (
    WindowClosedError,
    _run_reply_text,
    advance_run,
    answer_message,
    enroll,
    run_definition,
)
from apps.contacts.models import Contact
from apps.conversations.models import Conversation

WAIT_THEN_TAG = {
    "trigger": {"type": "manual"},
    "steps": [
        {"id": "wait", "type": "wait", "seconds": 3600, "next": "tag"},
        {"id": "tag", "type": "add_tag", "tag": "old", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ],
}


def _keyword_reply(text, words=("price",)):
    return {
        "trigger": {
            "type": "conversation.message_received",
            "match": {"mode": "contains", "any": list(words)},
        },
        "steps": [
            {"id": "reply", "type": "reply_text", "text": text, "next": "stop"},
            {"id": "stop", "type": "stop"},
        ],
    }


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971234567")


def _wf(account, definition, slug="wf", **kw):
    return Workflow.objects.create(
        account=account,
        name=slug,
        slug=slug,
        status=kw.pop("status", Workflow.Status.PUBLISHED),
        definition=definition,
        **kw,
    )


# ── Versions ────────────────────────────────────────────────────────────────


def test_a_run_finishes_on_the_version_it_started(account, contact):
    wf = _wf(account, WAIT_THEN_TAG)
    run = enroll(wf, contact)
    edited = {
        **WAIT_THEN_TAG,
        "steps": [
            {"id": "wait", "type": "wait", "seconds": 60, "next": "stop2"},
            {"id": "stop2", "type": "stop"},
        ],
    }
    automation_api.save_edits(wf, edited)
    automation_api.publish(wf)
    wf.refresh_from_db()
    assert wf.definition == edited
    run.refresh_from_db()
    assert run_definition(run) == WAIT_THEN_TAG, "the in-flight run keeps its steps"

    run.next_due_at = timezone.now() - timedelta(seconds=1)
    run.save(update_fields=["next_due_at"])
    advance_run(run)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    assert run.step_runs.filter(step_id="tag").exists(), "the old 'tag' step still ran"

    newer = enroll(wf, Contact.objects.create(account=account, phone="+260970000001"))
    assert run_definition(newer) == edited


def test_editing_a_live_workflow_keeps_it_running_until_publish(account):
    wf = _wf(account, WAIT_THEN_TAG)
    automation_api.save_edits(wf, {"trigger": {"type": "manual"}, "steps": []})
    wf.refresh_from_db()
    assert wf.definition == WAIT_THEN_TAG and wf.draft_definition is not None
    automation_api.discard_edits(wf)
    wf.refresh_from_db()
    assert wf.draft_definition is None


# ── Pause / resume / archive ─────────────────────────────────────────────────


def test_pause_holds_waiting_customers_and_resume_gives_them_their_time_back(
    account, contact
):
    wf = _wf(account, WAIT_THEN_TAG)
    run = enroll(wf, contact)
    run.refresh_from_db()
    due = run.next_due_at
    automation_api.pause(wf)
    wf.refresh_from_db()
    wf.paused_at = timezone.now() - timedelta(hours=2)
    wf.save(update_fields=["paused_at"])
    automation_api.resume(wf)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING
    assert run.next_due_at - due >= timedelta(hours=2) - timedelta(seconds=5)


def test_resume_fresh_lets_held_customers_go(account, contact):
    wf = _wf(account, WAIT_THEN_TAG)
    run = enroll(wf, contact)
    automation_api.pause(wf)
    automation_api.resume(wf, waiting="cancel")
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.CANCELLED


def test_archive_ends_runs_in_flight(account, contact):
    wf = _wf(account, WAIT_THEN_TAG)
    run = enroll(wf, contact)
    assert automation_api.archive(wf) == 1
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.CANCELLED


# ── Consent ──────────────────────────────────────────────────────────────────


def test_an_opted_out_customer_is_not_enrolled_in_a_whatsapp_automation(
    account, contact
):
    from apps.whatsapp.models import WhatsAppContact

    WhatsAppContact.objects.create(
        account=account,
        phone_number=contact.phone,
        contact=contact,
        opt_in_status="opted_out",
    )
    wf = _wf(
        account,
        {
            "trigger": {"type": "manual"},
            "steps": [{"id": "s", "type": "send_whatsapp", "template": "hi"}],
        },
    )
    assert enroll(wf, contact) is None
    tag_only = _wf(
        account,
        {
            "trigger": {"type": "manual"},
            "steps": [{"id": "t", "type": "add_tag", "tag": "x"}],
        },
        slug="tag",
    )
    assert enroll(tag_only, contact) is not None, "silent automations still run"


def test_a_stop_message_starts_nothing(account, contact):
    _wf(account, _keyword_reply("Our prices…", words=("stop",)))
    with mock.patch("apps.automation.workflow_engine._run_reply_text", return_value={}):
        assert (
            answer_message(account.pk, contact, context={"message": {"body": "STOP"}})
            is False
        )
    assert not WorkflowRun.objects.exists()


# ── Messaging window ─────────────────────────────────────────────────────────


def _closed_conversation():
    convo = mock.MagicMock()
    convo.channel = Conversation.Channel.WHATSAPP
    convo.Channel = Conversation.Channel
    return convo


def test_reply_outside_the_window_fails_with_a_fix(account, contact):
    wf = _wf(account, _keyword_reply("Hi"))
    run = WorkflowRun.objects.create(workflow=wf, contact=contact)
    with (
        mock.patch(
            "apps.automation.workflow_engine._conversation_for_run",
            return_value=_closed_conversation(),
        ),
        mock.patch(
            "apps.automation.workflow_engine._messaging_window_open", return_value=False
        ),
    ):
        with pytest.raises(WindowClosedError, match="fallback template"):
            _run_reply_text(run, {"id": "reply", "type": "reply_text", "text": "Hi"})


def test_reply_outside_the_window_uses_the_fallback_template(account, contact):
    wf = _wf(account, _keyword_reply("Hi"))
    run = WorkflowRun.objects.create(workflow=wf, contact=contact)
    with (
        mock.patch(
            "apps.automation.workflow_engine._conversation_for_run",
            return_value=_closed_conversation(),
        ),
        mock.patch(
            "apps.automation.workflow_engine._messaging_window_open", return_value=False
        ),
        mock.patch(
            "apps.automation.workflow_engine._run_send_whatsapp",
            return_value={"outbound_message_id": 7},
        ) as send,
    ):
        result = _run_reply_text(
            run,
            {
                "id": "reply",
                "type": "reply_text",
                "text": "Hi",
                "fallback_template": "checking_in",
            },
        )
    assert result["fallback_template"] == "checking_in"
    assert send.call_args.args[1]["template"] == "checking_in"


# ── One message, one answer ──────────────────────────────────────────────────


def test_only_the_highest_priority_workflow_answers(account, contact):
    _wf(account, _keyword_reply("Second"), slug="b", priority=200)
    _wf(account, _keyword_reply("First"), slug="a", priority=10)
    sent = []

    def fake_reply(run, step):
        sent.append(step["text"])
        return {}

    with mock.patch(
        "apps.automation.workflow_engine._run_reply_text", side_effect=fake_reply
    ):
        assert answer_message(
            account.pk, contact, context={"message": {"body": "price?"}}
        )
    assert sent == ["First"]


def test_silent_workflows_still_run_after_one_answered(account, contact):
    _wf(account, _keyword_reply("Our prices…"), slug="a", priority=10)
    _wf(
        account,
        {
            "trigger": {"type": "conversation.message_received"},
            "steps": [{"id": "t", "type": "add_tag", "tag": "asked", "next": ""}],
        },
        slug="tagger",
        priority=50,
    )
    with mock.patch("apps.automation.workflow_engine._run_reply_text", return_value={}):
        answer_message(account.pk, contact, context={"message": {"body": "price?"}})
    assert WorkflowRun.objects.filter(workflow__slug="tagger").exists()


# ── Tenancy and roles ────────────────────────────────────────────────────────


def test_a_business_event_never_enrolls_another_accounts_customer(account):
    from apps.automation.workflow_engine import on_business_event

    other = Account.objects.create(company_name="Other")
    stranger = Contact.objects.create(account=other, phone="+260979999999")
    _wf(
        account,
        {
            "trigger": {"type": "business_event", "name": "order.completed"},
            "steps": [{"id": "t", "type": "add_tag", "tag": "x"}],
        },
    )
    event = mock.MagicMock(
        contact_id=stranger.pk, account_id=account.pk, name="order.completed", data={}
    )
    on_business_event(event)
    assert not WorkflowRun.objects.exists()


def test_a_member_cannot_turn_automations_on(client, account):
    user = User.objects.create_user("m", "m@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.MEMBER)
    client.force_login(user)
    wf = _wf(account, WAIT_THEN_TAG, status=Workflow.Status.DRAFT)
    client.post(f"/automations/{wf.slug}/publish/")
    wf.refresh_from_db()
    assert wf.status == Workflow.Status.DRAFT


# ── Failures reach the owner ─────────────────────────────────────────────────


def test_a_late_whatsapp_failure_alerts_and_closes_open_questions(account, contact):
    from apps.automation.integrations.whatsapp import mark_outbound_message_failed
    from apps.automation.models import WorkflowStepRun

    wf = _wf(account, WAIT_THEN_TAG)
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.WAITING
    )
    convo = Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )
    WorkflowInteraction.objects.create(run=run, step_id="ask", conversation=convo)
    message = mock.MagicMock(id=42, last_error="(#131047) Re-engagement message")
    with mock.patch.object(WorkflowStepRun.objects, "filter") as flt:
        flt.return_value.exclude.return_value.select_related.return_value.first.return_value = mock.MagicMock(
            run=run, result={}
        )
        with mock.patch("apps.automation.workflow_engine._alert_failure") as alert:
            mark_outbound_message_failed(message)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED
    alert.assert_called_once()
    assert not WorkflowInteraction.objects.filter(
        run=run, status=WorkflowInteraction.Status.OPEN
    ).exists()

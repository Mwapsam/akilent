"""Review fixes (2026-10-25): holds reach every path that moves a run, automatic sends
don't count as a teammate, a failed form launch leaves nothing behind."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation import api as automation_api
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import HELD_BY_PAUSE, enroll, run_due
from apps.contacts.models import Contact
from apps.conversations import followups
from apps.conversations import forms as conversation_forms
from apps.conversations.models import (
    Conversation,
    ConversationForm,
    FollowUp,
    FormResponse,
)


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Mwamba Kitchen")


@pytest.fixture
def conversation(account):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    return Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP
    )


def _wf(account, steps, slug="wf"):
    return Workflow.objects.create(
        account=account,
        name=slug,
        slug=slug,
        status=Workflow.Status.PUBLISHED,
        definition={"trigger": {"type": "manual"}, "steps": steps},
    )


def _start(wf, conversation):
    return enroll(
        wf,
        conversation.contact,
        context={"conversation_id": conversation.public_id, "message": {"body": "hi"}},
    )


def _text_form(account):
    return ConversationForm.objects.create(
        account=account,
        name="Budget",
        status=ConversationForm.Status.PUBLISHED,
        presentation=ConversationForm.Presentation.TEXT,
        questions=[{"key": "budget", "label": "Budget", "field_type": "number"}],
    )


def test_a_finished_form_does_not_move_a_paused_workflow(account, conversation):
    form = _text_form(account)
    wf = _wf(
        account,
        [
            {
                "id": "ask",
                "type": "start_form",
                "form": form.public_id,
                "next": "tag",
                "on_timeout": "",
            },
            {"id": "tag", "type": "add_tag", "tag": "answered"},
        ],
    )
    with patch("apps.conversations.forms._send"):
        run = _start(wf, conversation)
        automation_api.pause(wf)
        conversation_forms.record_answer(conversation, "300")
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.ACTIVE and run.current_step == "tag"
    assert not conversation.contact.tags.filter(slug="answered").exists()

    wf.refresh_from_db()
    automation_api.resume(wf)
    WorkflowRun.objects.filter(pk=run.pk).update(
        next_due_at=timezone.now() - timedelta(minutes=2)
    )
    run_due()
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    assert conversation.contact.tags.filter(slug="answered").exists()


def test_a_timer_waits_out_a_teammate_takeover(account, conversation):
    wf = _wf(
        account,
        [
            {"id": "w", "type": "wait", "seconds": 60, "next": "tag"},
            {"id": "tag", "type": "add_tag", "tag": "nudged"},
        ],
    )
    run = _start(wf, conversation)
    WorkflowRun.objects.filter(pk=run.pk).update(
        next_due_at=timezone.now() - timedelta(minutes=1)
    )
    conversation.pause_automation(4)
    run_due()
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING
    assert run.next_due_at == conversation.automation_paused_until
    assert run.context[HELD_BY_PAUSE] is True
    assert not conversation.contact.tags.filter(slug="nudged").exists()

    conversation.resume_automation()
    run_due()
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    assert HELD_BY_PAUSE not in run.context
    assert conversation.contact.tags.filter(slug="nudged").exists()


def test_turning_a_pause_off_leaves_longer_waits_alone(account, conversation):
    wf = _wf(account, [{"id": "w", "type": "wait", "seconds": 3 * 86400, "next": ""}])
    run = _start(wf, conversation)
    due = run.next_due_at
    conversation.pause_automation(4)
    conversation.resume_automation()
    run.refresh_from_db()
    assert run.next_due_at == due


def test_enrolling_someone_already_in_it_skips_the_snapshot(account, conversation):
    wf = _wf(account, [{"id": "w", "type": "wait", "seconds": 3600, "next": ""}])
    first = _start(wf, conversation)
    with patch("apps.automation.workflow_engine.snapshot") as snapshot:
        again = _start(wf, conversation)
    assert again.pk == first.pk
    snapshot.assert_not_called()


@pytest.mark.parametrize(
    "key, who",
    [
        ("form:7:q0", "automation"),
        ("campaign:1:recipient:2", "system"),
        ("manual-1", ""),
    ],
)
def test_form_and_campaign_sends_are_marked_automatic(key, who):
    from apps.whatsapp.tasks import _sender_of

    assert _sender_of(SimpleNamespace(payload={}, idempotency_key=key)) == who


def test_an_automatic_send_neither_pauses_nor_closes_reminders(account, conversation):
    from apps.conversations.services import record_outbound_message

    account.pause_automation_hours = 4
    account.save(update_fields=["pause_automation_hours"])
    reminder, _ = followups.create(
        account,
        conversation.contact,
        conversation=conversation,
        due_at=timezone.now() + timedelta(hours=1),
        source=FollowUp.Source.MISSED,
    )
    record_outbound_message(
        conversation=conversation,
        body="Budget?",
        timestamp=timezone.now(),
        status="sent",
        metadata={"sent_by": "automation"},
    )
    conversation.refresh_from_db()
    reminder.refresh_from_db()
    assert not conversation.automation_paused
    assert reminder.done_at is None

    record_outbound_message(
        conversation=conversation,
        body="I'll check",
        timestamp=timezone.now(),
        status="sent",
    )
    conversation.refresh_from_db()
    reminder.refresh_from_db()
    assert conversation.automation_paused
    assert reminder.done_at is not None


def test_a_form_question_that_cannot_be_sent_leaves_no_form_in_progress(
    account, conversation
):
    from apps.conversations.actions import ActionError

    form = _text_form(account)
    with patch(
        "apps.conversations.actions.run_action",
        side_effect=ActionError("window closed"),
    ):
        with pytest.raises(conversation_forms.FormError):
            conversation_forms.start_form(conversation, form)
    assert not FormResponse.objects.exists()


def test_form_questions_are_sent_as_the_automation(account, conversation):
    form = _text_form(account)
    with patch("apps.conversations.actions.run_action") as run_action:
        conversation_forms.start_form(conversation, form)
    assert run_action.call_args.kwargs["sent_by"] == "automation"


def test_stale_forms_are_found_behind_fresh_ones(account, conversation):
    form = _text_form(account)
    stale = FormResponse.objects.create(
        account=account,
        form=form,
        conversation=conversation,
        contact=conversation.contact,
    )
    FormResponse.objects.filter(pk=stale.pk).update(
        started_at=timezone.now() - timedelta(days=5)
    )
    assert conversation_forms.abandon_stale() == 1
    stale.refresh_from_db()
    assert stale.status == FormResponse.Status.ABANDONED


def test_reassigning_to_a_garbled_person_is_a_message_not_a_crash(
    client, account, conversation
):
    user = User.objects.create_user("o", "o@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    reminder, _ = followups.create(
        account, conversation.contact, conversation=conversation, due_at=timezone.now()
    )
    response = client.post(f"/inbox/followups/{reminder.pk}/reassign/", {"user": "abc"})
    assert response.status_code in (200, 302)
    reminder.refresh_from_db()
    assert reminder.assigned_to is None


def test_due_reminders_are_found_with_their_people(account, conversation):
    user = User.objects.create_user("a", "a@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    followups.create(
        account,
        conversation.contact,
        conversation=conversation,
        due_at=timezone.now() - timedelta(minutes=1),
        assigned_to=user,
    )
    followups.create(
        account, conversation.contact, due_at=timezone.now() - timedelta(minutes=1)
    )
    with patch("apps.core.realtime.publish") as publish:
        assert followups.notify_due() == 2
    assert publish.call_count == 2

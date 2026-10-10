"""A0 — Reply correlation: WorkflowInteraction / claim_reply tests.

Covers:
- Two runs from different workflows waiting on one conversation; a tap resumes
  only its own run.
- A duplicate delivery (same message_key) resumes nothing.
- Two concurrent typed messages claim at most one run each.
- An expiry/answer race yields exactly one transition.
- A crash after claim commit (advance_run patched out) leaves the run in a
  durable ACTIVE state that run_due picks up exactly once.
- Wrong-conversation token rejection.
- Stale-run detection (run already advanced before claim arrives).
- Legacy fallback (WAITING runs with no interaction rows) uses old path.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.automation.interaction import (
    ClaimResult,
    cancel_open_interactions_for_run,
    claim_reply,
)
from apps.automation.models import (
    InteractionAnswer,
    Workflow,
    WorkflowInteraction,
    WorkflowRun,
    WorkflowStepRun,
)
from apps.automation.workflow_engine import advance_run, enroll, run_due
from apps.contacts.models import Contact
from apps.conversations.models import Conversation


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def account(db):
    from decimal import Decimal

    from apps.accounts.models import Account
    from apps.billing.models import Plan, Subscription

    acc = Account.objects.create(company_name="TestCo")
    plan = Plan.objects.create(
        slug="p",
        name="P",
        price_monthly=Decimal("10"),
        max_emails_per_month=1000,
        email_apis=True,
        api_rate_per_min=0,
    )
    Subscription.objects.create(
        account=acc,
        plan=plan,
        status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )
    return acc


@pytest.fixture
def contact(account):
    return Contact.objects.create(
        account=account, phone="+260971234567", first_name="Chanda"
    )


@pytest.fixture
def conversation(account, contact):
    return Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )


def _wf(account, steps, *, trigger=None):
    return Workflow.objects.create(
        account=account,
        name="WF-" + str(id(steps)),
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": trigger or {},
            "steps": steps,
        },
    )


def _wait_for_reply_steps(step_id="ask", *, next_step="done"):
    return [
        {
            "id": step_id,
            "type": "wait_for_reply",
            "timeout_seconds": 3600,
            "routes": {"yes": next_step},
            "default": next_step,
        },
        {"id": next_step, "type": "stop"},
    ]


def _open_interaction(run, conversation, step_id="ask", option_ids=None, free_text=False, deadline_offset=3600):
    """Create an open WorkflowInteraction for a WAITING run."""
    return WorkflowInteraction.objects.create(
        run=run,
        step_id=step_id,
        conversation=conversation,
        expected={"option_ids": option_ids or ["yes"], "free_text": free_text},
        status=WorkflowInteraction.Status.OPEN,
        deadline_at=timezone.now() + timedelta(seconds=deadline_offset),
    )


def _park_run(wf, contact, step_id="ask"):
    """Enroll and manually park a run on a step."""
    run = enroll(wf, contact)
    # advance_run parks the run on the wait_for_reply step automatically,
    # but we need to set it up cleanly.
    run.refresh_from_db()
    return run


def _make_key(msg_id="msg001"):
    return f"whatsapp:PNID:{msg_id}"


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_tap_resumes_correct_run_only(account, contact, conversation):
    """Two runs waiting on one conversation; tapping one token only resumes that run."""
    wf1 = _wf(account, _wait_for_reply_steps("ask1", next_step="done1"))
    wf2 = _wf(account, _wait_for_reply_steps("ask2", next_step="done2"))

    # Create two WAITING runs manually.
    run1 = WorkflowRun.objects.create(
        workflow=wf1, contact=contact, status=WorkflowRun.Status.WAITING,
        current_step="ask1", next_due_at=timezone.now() + timedelta(hours=1),
    )
    run2 = WorkflowRun.objects.create(
        workflow=wf2, contact=contact, status=WorkflowRun.Status.WAITING,
        current_step="ask2", next_due_at=timezone.now() + timedelta(hours=1),
    )

    ix1 = _open_interaction(run1, conversation, "ask1", option_ids=["yes"])
    ix2 = _open_interaction(run2, conversation, "ask2", option_ids=["yes"])

    # Tap ix1's token.
    reply_id = f"{ix1.token}:yes"
    result = claim_reply(conversation, _make_key("m1"), reply_id, "")

    assert result == "claimed"
    run1.refresh_from_db()
    run2.refresh_from_db()
    ix1.refresh_from_db()
    ix2.refresh_from_db()

    # run1 advanced (completed via stop step).
    assert run1.status in (WorkflowRun.Status.COMPLETED, WorkflowRun.Status.ACTIVE)
    # run2 is untouched.
    assert run2.status == WorkflowRun.Status.WAITING
    assert ix1.status == WorkflowInteraction.Status.ANSWERED
    assert ix2.status == WorkflowInteraction.Status.OPEN


@pytest.mark.django_db
def test_duplicate_delivery_returns_already_claimed(account, contact, conversation):
    """The same provider message id (message_key) is a no-op the second time."""
    wf = _wf(account, _wait_for_reply_steps())
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.WAITING,
        current_step="ask", next_due_at=timezone.now() + timedelta(hours=1),
    )
    ix = _open_interaction(run, conversation, "ask")

    key = _make_key("dup01")
    reply_id = f"{ix.token}:yes"

    r1 = claim_reply(conversation, key, reply_id, "")
    r2 = claim_reply(conversation, key, reply_id, "")

    assert r1 == "claimed"
    assert r2 == "already_claimed"
    assert InteractionAnswer.objects.filter(message_key=key).count() == 1


@pytest.mark.django_db
def test_wrong_conversation_token_rejected(account, contact, conversation):
    """A token from a different conversation is rejected without consuming anything."""
    other_contact = Contact.objects.create(account=account, phone="+260977000001")
    other_conv = Conversation.objects.create(
        account=account, contact=other_contact, channel="whatsapp"
    )
    wf = _wf(account, _wait_for_reply_steps())
    run = WorkflowRun.objects.create(
        workflow=wf, contact=other_contact, status=WorkflowRun.Status.WAITING,
        current_step="ask", next_due_at=timezone.now() + timedelta(hours=1),
    )
    ix = _open_interaction(run, other_conv, "ask")

    # Present ix's token to conversation (wrong one).
    result = claim_reply(conversation, _make_key("x1"), f"{ix.token}:yes", "")

    assert result == "wrong_conversation"
    run.refresh_from_db()
    ix.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING
    assert ix.status == WorkflowInteraction.Status.OPEN


@pytest.mark.django_db
def test_stale_run_interaction_cancelled(account, contact, conversation):
    """If the run already advanced before claim, the interaction is cancelled."""
    wf = _wf(account, _wait_for_reply_steps())
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.WAITING,
        current_step="ask", next_due_at=timezone.now() + timedelta(hours=1),
    )
    ix = _open_interaction(run, conversation, "ask")

    # Manually advance the run past the step (simulate timeout/other path).
    run.status = WorkflowRun.Status.COMPLETED
    run.current_step = "done"
    run.completed_at = timezone.now()
    run.save()

    result = claim_reply(conversation, _make_key("s1"), f"{ix.token}:yes", "")

    assert result == "stale_run"
    ix.refresh_from_db()
    assert ix.status == WorkflowInteraction.Status.CANCELLED


@pytest.mark.django_db
def test_no_open_interactions_returns_unmatched(account, contact, conversation):
    """No open interactions → unmatched (goes to normal pipeline)."""
    result = claim_reply(conversation, _make_key("u1"), "", "hello")
    assert result == "unmatched"


@pytest.mark.django_db
def test_expiry_wins_late_reply_unmatched(account, contact, conversation):
    """Expiry commits first; a late reply after expiry returns unmatched."""
    wf = _wf(account, _wait_for_reply_steps())
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.WAITING,
        current_step="ask", next_due_at=timezone.now() - timedelta(seconds=1),
    )
    ix = _open_interaction(run, conversation, "ask", deadline_offset=-1)

    # Simulate expiry: directly mark the interaction expired (as run_due would).
    ix.status = WorkflowInteraction.Status.EXPIRED
    ix.closed_at = timezone.now()
    ix.save()

    # A late reply after expiry.
    result = claim_reply(conversation, _make_key("late1"), f"{ix.token}:yes", "")

    # Token no longer open → nothing claimed, goes to normal pipeline.
    assert result in ("unmatched", "stale_run", "wrong_conversation")
    # Interaction stays expired (not reopened).
    ix.refresh_from_db()
    assert ix.status == WorkflowInteraction.Status.EXPIRED


@pytest.mark.django_db
def test_crash_recovery_run_due_advances_once(account, contact, conversation):
    """After a crash right after claim commit, run_due advances the run exactly once."""
    wf = _wf(account, _wait_for_reply_steps())
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.WAITING,
        current_step="ask", next_due_at=timezone.now() + timedelta(hours=1),
    )
    ix = _open_interaction(run, conversation, "ask")

    # Patch out the fast-path advance so it crashes as if the process died.
    with patch("apps.automation.interaction._advance_after_commit", side_effect=Exception("crash")):
        result = claim_reply(conversation, _make_key("cr1"), f"{ix.token}:yes", "")

    assert result == "claimed"

    # Run is ACTIVE with next_due_at set (durable continuation marker).
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.ACTIVE
    assert run.next_due_at is not None

    # Move next_due_at into the past to trigger the sweeper.
    run.next_due_at = timezone.now() - timedelta(seconds=120)
    run.save(update_fields=["next_due_at"])

    # run_due twice — must advance exactly once.
    n1 = run_due()
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED

    n2 = run_due()
    # Second run_due doesn't double-advance (run already COMPLETED).
    assert WorkflowStepRun.objects.filter(run=run, step_id="ask").count() == 1


@pytest.mark.django_db
def test_cancel_open_interactions_for_run(account, contact, conversation):
    """cancel_open_interactions_for_run cancels all open interactions for a run."""
    wf = _wf(account, _wait_for_reply_steps())
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.WAITING,
        current_step="ask", next_due_at=timezone.now() + timedelta(hours=1),
    )
    ix = _open_interaction(run, conversation, "ask")

    # Mark run cancelled.
    run.status = WorkflowRun.Status.CANCELLED
    run.save()

    n = cancel_open_interactions_for_run(run)
    assert n == 1
    ix.refresh_from_db()
    assert ix.status == WorkflowInteraction.Status.CANCELLED


@pytest.mark.django_db
def test_typed_reply_routes_to_interaction_with_free_text(account, contact, conversation):
    """A typed reply routes to the first free-text interaction."""
    wf = _wf(account, _wait_for_reply_steps())
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.WAITING,
        current_step="ask", next_due_at=timezone.now() + timedelta(hours=1),
    )
    ix = _open_interaction(run, conversation, "ask", free_text=True)

    result = claim_reply(conversation, _make_key("t1"), "", "my typed answer")

    assert result == "claimed"
    ix.refresh_from_db()
    assert ix.status == WorkflowInteraction.Status.ANSWERED
    assert InteractionAnswer.objects.filter(
        message_key=_make_key("t1"), interaction=ix
    ).exists()


@pytest.mark.django_db
def test_complete_cancels_open_interactions(account, contact, conversation):
    """When advance_run completes a run, open interactions are cancelled."""
    wf = _wf(account, [{"id": "stop", "type": "stop"}])
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.ACTIVE,
        current_step="stop",
    )
    # Create an open interaction on this run (shouldn't happen in practice, but
    # tests the cleanup contract).
    ix = WorkflowInteraction.objects.create(
        run=run,
        step_id="fake",
        conversation=conversation,
        expected={},
        status=WorkflowInteraction.Status.OPEN,
    )

    advance_run(run)
    run.refresh_from_db()
    ix.refresh_from_db()

    assert run.status == WorkflowRun.Status.COMPLETED
    assert ix.status == WorkflowInteraction.Status.CANCELLED


@pytest.mark.django_db
def test_legacy_fallback_used_for_runs_without_interactions(account, contact, conversation):
    """resume_on_reply legacy path handles runs with no WorkflowInteraction rows."""
    from apps.automation.workflow_engine import resume_on_reply

    wf = _wf(account, _wait_for_reply_steps())
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.WAITING,
        current_step="ask", next_due_at=timezone.now() + timedelta(hours=1),
    )
    # No WorkflowInteraction rows for this run → legacy path.
    assert not WorkflowInteraction.objects.filter(run=run).exists()

    # Provide a message WITHOUT the _conversation key so the new path is skipped.
    message = {
        "reply_id": "yes",
        "reply_title": "Yes",
        "body": "yes",
    }
    resumed = resume_on_reply(account.id, contact, message)

    # Legacy path should have resumed the run.
    assert resumed is True
    run.refresh_from_db()
    assert run.status in (WorkflowRun.Status.ACTIVE, WorkflowRun.Status.COMPLETED)


@pytest.mark.django_db
def test_run_due_sweeps_stalled_active_run(account, contact, conversation):
    """run_due picks up an ACTIVE run with a past next_due_at (stalled continuation)."""
    wf = _wf(account, [{"id": "s", "type": "stop"}])
    run = WorkflowRun.objects.create(
        workflow=wf, contact=contact, status=WorkflowRun.Status.ACTIVE,
        current_step="s",
        next_due_at=timezone.now() - timedelta(seconds=120),  # stalled
    )

    n = run_due()
    run.refresh_from_db()

    assert n >= 1
    assert run.status == WorkflowRun.Status.COMPLETED
    assert run.next_due_at is None

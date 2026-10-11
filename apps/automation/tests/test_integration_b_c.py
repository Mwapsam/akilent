"""Cross-app integration test: inbound message → claim → workflow resume → attribute mutation.

This is the plan's explicit release blocker for Workstreams B and C:
  create_lead (CRM) → enroll_for_trigger (lead.created) → advance_run →
  ask_question step opens WorkflowInteraction → claim_reply (typed answer) →
  set_attributes writes the attribute → attribute.changed event emitted →
  run transitions to next step.

Tests in this file cross apps/crm, apps/automation, apps/contacts, and apps/logs.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.automation.interaction import claim_reply
from apps.automation.models import (
    InteractionAnswer,
    Workflow,
    WorkflowInteraction,
    WorkflowRun,
)
from apps.contacts.models import Contact, CustomAttributeDef
from apps.conversations.models import Conversation

_WINDOW_OPEN = "apps.automation.workflow_engine._messaging_window_open"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def account(db):
    from apps.accounts.models import Account
    from apps.billing.models import Plan, Subscription

    acc = Account.objects.create(company_name="TestCo-INT")
    plan = Plan.objects.create(
        slug="pint",
        name="PInt",
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
        account=account, phone="+260977100001", first_name="Kapambwe"
    )


@pytest.fixture
def conversation(account, contact):
    return Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )


@pytest.fixture
def budget_def(account):
    return CustomAttributeDef.objects.create(
        account=account, entity="lead", key="budget", type="string"
    )


@pytest.fixture
def timeline_def(account):
    return CustomAttributeDef.objects.create(
        account=account, entity="lead", key="timeline", type="string"
    )


def _qualification_workflow(account):
    """A two-question lead-qualification workflow."""
    return Workflow.objects.create(
        account=account,
        name="Lead Qualification",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "lead.created"},
            "steps": [
                {
                    "id": "q_budget",
                    "type": "ask_question",
                    "question": "What is your approximate budget?",
                    "reprompt": "Please give a number, e.g. K500.",
                    "attribute": "budget",
                    "target": "lead",
                    "max_attempts": 2,
                    "timeout_seconds": 3600,
                    "next": "q_timeline",
                    "on_timeout": "notify",
                    "on_invalid": "notify",
                    "on_error": "notify",
                },
                {
                    "id": "q_timeline",
                    "type": "ask_question",
                    "question": "When do you need this?",
                    "reprompt": "Please give a timeframe.",
                    "attribute": "timeline",
                    "target": "lead",
                    "max_attempts": 2,
                    "timeout_seconds": 3600,
                    "next": "done",
                    "on_timeout": "notify",
                    "on_invalid": "notify",
                    "on_error": "notify",
                },
                {"id": "notify", "type": "stop"},
                {"id": "done", "type": "stop"},
            ],
        },
    )


# ── INT.1  Full lead-qualification happy path ─────────────────────────────────


@pytest.mark.django_db(transaction=True)
def test_lead_created_enrolls_and_ask_question_parks_run(
    account, contact, conversation, budget_def, timeline_def
):
    """create_lead → enroll → ask_question step → WorkflowInteraction opened,
    run WAITING. Proves the CRM→automation→interaction chain is wired."""
    from apps.automation.workflow_engine import enroll
    from apps.crm.models import Lead

    wf = _qualification_workflow(account)
    lead = Lead.objects.create(account=account, contact=contact)

    with patch("apps.core.actions.run_action"), patch(_WINDOW_OPEN, return_value=True):
        run = enroll(wf, contact, context={"lead_id": str(lead.public_id)})

    assert run is not None
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING
    assert run.current_step == "q_budget"

    interaction = WorkflowInteraction.objects.get(run=run, step_id="q_budget")
    assert interaction.status == WorkflowInteraction.Status.OPEN
    assert interaction.conversation_id == conversation.pk


@pytest.mark.django_db(transaction=True)
def test_claim_reply_writes_attribute_and_advances_to_next_question(
    account, contact, conversation, budget_def, timeline_def
):
    """claim_reply with a valid answer → set_attributes writes the attribute,
    attribute.changed event emitted, run advances to q_timeline (WAITING)."""
    from apps.automation.workflow_engine import enroll
    from apps.conversations.models import Event
    from apps.crm.models import Lead

    wf = _qualification_workflow(account)
    lead = Lead.objects.create(account=account, contact=contact)

    with patch("apps.core.actions.run_action"), patch(_WINDOW_OPEN, return_value=True):
        run = enroll(wf, contact, context={"lead_id": str(lead.public_id)})

    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING

    msg_key = "whatsapp:PNID:int_msg_001"
    with patch("apps.core.actions.run_action"), patch(_WINDOW_OPEN, return_value=True):
        result = claim_reply(conversation, msg_key, None, "K1000")

    assert result == "claimed"

    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING
    assert run.current_step == "q_timeline"

    # Attribute written.
    lead.refresh_from_db()
    assert lead.attributes.get("budget") == "K1000"

    # InteractionAnswer durable record exists.
    assert InteractionAnswer.objects.filter(message_key=msg_key).exists()

    # attribute.changed event emitted.
    assert Event.objects.filter(account=account, type="attribute.changed").exists()

    # First interaction is answered; second is open.
    assert WorkflowInteraction.objects.filter(
        run=run, step_id="q_budget", status=WorkflowInteraction.Status.ANSWERED
    ).exists()
    assert WorkflowInteraction.objects.filter(
        run=run, step_id="q_timeline", status=WorkflowInteraction.Status.OPEN
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_second_answer_completes_qualification(
    account, contact, conversation, budget_def, timeline_def
):
    """Answer both questions → both attributes written, run reaches DONE/stop step."""
    from apps.automation.workflow_engine import enroll
    from apps.crm.models import Lead

    wf = _qualification_workflow(account)
    lead = Lead.objects.create(account=account, contact=contact)

    with patch("apps.core.actions.run_action"), patch(_WINDOW_OPEN, return_value=True):
        run = enroll(wf, contact, context={"lead_id": str(lead.public_id)})

    # Answer q_budget.
    with patch("apps.core.actions.run_action"), patch(_WINDOW_OPEN, return_value=True):
        r1 = claim_reply(conversation, "whatsapp:PNID:int_msg_002", None, "K2000")
    assert r1 == "claimed"

    # Answer q_timeline.
    with patch("apps.core.actions.run_action"), patch(_WINDOW_OPEN, return_value=True):
        r2 = claim_reply(conversation, "whatsapp:PNID:int_msg_003", None, "3 months")
    assert r2 == "claimed"

    lead.refresh_from_db()
    assert lead.attributes.get("budget") == "K2000"
    assert lead.attributes.get("timeline") == "3 months"

    run.refresh_from_db()
    # Run completes at the "done" stop step.
    assert run.status == WorkflowRun.Status.COMPLETED


@pytest.mark.django_db(transaction=True)
def test_duplicate_inbound_message_is_idempotent(
    account, contact, conversation, budget_def, timeline_def
):
    """The same provider message_key arriving twice must not write the attribute
    twice or advance the run twice (already_claimed on the second delivery)."""
    from apps.automation.workflow_engine import enroll
    from apps.crm.models import Lead

    wf = _qualification_workflow(account)
    lead = Lead.objects.create(account=account, contact=contact)

    with patch("apps.core.actions.run_action"), patch(_WINDOW_OPEN, return_value=True):
        run = enroll(wf, contact, context={"lead_id": str(lead.public_id)})

    msg_key = "whatsapp:PNID:int_msg_004"
    with patch("apps.core.actions.run_action"), patch(_WINDOW_OPEN, return_value=True):
        r1 = claim_reply(conversation, msg_key, None, "K500")
    assert r1 == "claimed"

    # Second delivery of the same message.
    r2 = claim_reply(conversation, msg_key, None, "K500")
    assert r2 == "already_claimed"

    # Run is at q_timeline, not further.
    run.refresh_from_db()
    assert run.current_step == "q_timeline"

    # Only one answer record.
    assert InteractionAnswer.objects.filter(message_key=msg_key).count() == 1


@pytest.mark.django_db(transaction=True)
def test_archived_def_prevents_attribute_write(
    account, contact, conversation, budget_def, timeline_def
):
    """If the attribute def is archived after the run parks but before the answer
    arrives, the attribute must NOT be written (def not found = invalid answer).
    The run stays WAITING and does not advance to q_timeline."""
    from datetime import timedelta

    from apps.contacts.attributes import archive_attribute_def
    from apps.crm.models import Lead

    # Build a DRAFT workflow (so it's not blocked from archiving by publish check).
    wf = _qualification_workflow(account)
    wf.status = Workflow.Status.DRAFT
    wf.save(update_fields=["status"])

    lead = Lead.objects.create(account=account, contact=contact)

    # Manually park a WAITING run (enroll ignores DRAFT workflows).
    run = WorkflowRun.objects.create(
        workflow=wf,
        contact=contact,
        status=WorkflowRun.Status.WAITING,
        current_step="q_budget",
        context={"lead_id": str(lead.public_id)},
        next_due_at=timezone.now() + timedelta(hours=1),
    )
    deadline = timezone.now() + timedelta(hours=1)
    WorkflowInteraction.objects.create(
        run=run,
        step_id="q_budget",
        conversation=conversation,
        expected={"free_text": True, "attribute": "budget", "target": "lead"},
        status=WorkflowInteraction.Status.OPEN,
        deadline_at=deadline,
    )
    run.next_due_at = deadline
    run.save(update_fields=["next_due_at"])

    # Terminate the run so it doesn't block the archive.
    run.status = WorkflowRun.Status.FAILED
    run.save(update_fields=["status"])

    # Archive the def — no published workflow or active/waiting run blocks it now.
    archive_attribute_def(budget_def)
    budget_def.refresh_from_db()
    assert budget_def.archived_at is not None

    # Create a fresh WAITING run with an open interaction now that the def is archived.
    run2 = WorkflowRun.objects.create(
        workflow=wf,
        contact=contact,
        status=WorkflowRun.Status.WAITING,
        current_step="q_budget",
        context={"lead_id": str(lead.public_id)},
        next_due_at=timezone.now() + timedelta(hours=1),
    )
    deadline2 = timezone.now() + timedelta(hours=1)
    WorkflowInteraction.objects.create(
        run=run2,
        step_id="q_budget",
        conversation=conversation,
        expected={"free_text": True, "attribute": "budget", "target": "lead"},
        status=WorkflowInteraction.Status.OPEN,
        deadline_at=deadline2,
    )
    run2.next_due_at = deadline2
    run2.save(update_fields=["next_due_at"])

    # A reply arrives: def not found (archived) → treated as invalid → re-prompt.
    with patch("apps.core.actions.run_action"):
        result = claim_reply(conversation, "whatsapp:PNID:int_msg_005", None, "K750")

    assert result == "claimed"

    # Attribute was NOT written.
    lead.refresh_from_db()
    assert "budget" not in (lead.attributes or {})

    # Run did NOT advance to q_timeline — still on q_budget (re-prompt path).
    run2.refresh_from_db()
    assert run2.current_step == "q_budget"
    assert run2.status == WorkflowRun.Status.WAITING

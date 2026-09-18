"""Phase 4: the generic ``action`` Workflow step, calling through the shared
Action Registry (apps.core.actions) instead of a hand-written per-type
function.
"""
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import enroll, validate_definition
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, ConversationNote
from apps.crm.models import Deal, Lead
from apps.crm.services import convert_lead_to_deal, create_lead
from apps.whatsapp.models import Conversation as WhatsAppConversation
from apps.whatsapp.models import WhatsAppContact


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Acme")


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, phone="+260971111111")


def _wf(account, steps, trigger=None):
    return Workflow.objects.create(
        account=account, name="WF", status=Workflow.Status.PUBLISHED,
        definition={"trigger": trigger or {"type": "manual"}, "steps": steps},
    )


@pytest.mark.django_db
def test_action_step_creates_lead_via_registry(account, contact):
    wf = _wf(account, [
        {"id": "a", "type": "action", "action": "create_lead", "params": {"source": "context.source"}, "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact, context={"source": "whatsapp"})
    run.refresh_from_db()

    assert run.status == WorkflowRun.Status.COMPLETED
    lead = Lead.objects.get(account=account, contact=contact)
    assert lead.source == "whatsapp"
    assert run.step_runs.get(step_id="a").result["lead_id"] == lead.public_id


@pytest.mark.django_db
def test_action_step_resolves_object_kwarg_by_id_from_context(account, contact):
    wa_contact = WhatsAppContact.objects.create(account=account, phone_number="+260971111111", contact=contact)
    wa_conversation = WhatsAppConversation.get_or_open(wa_contact)
    conversation = Conversation.objects.create(
        account=account, contact=contact, channel=Conversation.Channel.WHATSAPP,
        whatsapp_conversation=wa_conversation,
    )

    wf = _wf(account, [
        {
            "id": "a", "type": "action", "action": "add_internal_note",
            "params": {"conversation_id": "context.conversation_id", "body": "Hello from a workflow"},
            "next": "b",
        },
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact, context={"conversation_id": conversation.public_id})
    run.refresh_from_db()

    assert run.status == WorkflowRun.Status.COMPLETED
    assert ConversationNote.objects.filter(conversation=conversation, body="Hello from a workflow").exists()


@pytest.mark.django_db
def test_action_step_fails_run_on_unknown_action(account, contact):
    wf = _wf(account, [
        {"id": "a", "type": "action", "action": "does_not_exist", "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED


@pytest.mark.django_db
def test_action_step_fails_run_on_missing_required_param(account, contact):
    # "create_lead" only auto-injects account/contact — "source" is optional
    # so this should succeed; force a failure via change_deal_stage, whose
    # "stage" has no object resolver and is required.
    wf = _wf(account, [
        {"id": "a", "type": "action", "action": "change_deal_stage", "params": {"deal_id": "context.deal_id"}, "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    lead = create_lead(account, contact)
    deal = convert_lead_to_deal(lead)
    run = enroll(wf, contact, context={"deal_id": deal.public_id})
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED


@pytest.mark.django_db
def test_validate_definition_rejects_unregistered_action(account):
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "action", "action": "not_a_real_action"}],
    })
    assert any(e["field"] == "action" for e in errors)


@pytest.mark.django_db
def test_validate_definition_rejects_missing_required_param(account):
    errors = validate_definition({
        "trigger": {"type": "manual"},
        # "add_internal_note" requires "conversation" and "body" — neither given.
        "steps": [{"id": "a", "type": "action", "action": "add_internal_note"}],
    })
    assert any(e["field"] == "params" for e in errors)


@pytest.mark.django_db
def test_validate_definition_accepts_well_formed_action_step(account):
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [
            {
                "id": "a", "type": "action", "action": "add_internal_note",
                "params": {"conversation_id": "context.conversation_id", "body": "hi"},
                "next": "b",
            },
            {"id": "b", "type": "stop"},
        ],
    })
    assert errors == []

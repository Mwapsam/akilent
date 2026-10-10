"""Workstream A — Instagram interactive flows.

Covers:
- IG send_buttons: quick replies sent, WorkflowInteraction created, run WAITING.
- IG inbound tap: quick_reply.payload parsed → claim_reply resumes correct run.
- IG timeout: WAITING run past deadline takes on_timeout path.
- WA send_buttons unchanged: no interaction created, wait_for_reply parks run.
- Publish validation: send_list rejected for IG channel trigger; button count/title limits.
- IG outbound failure: mark_outbound_message_failed cancels interaction + fails run.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.automation.models import (
    Workflow,
    WorkflowInteraction,
    WorkflowRun,
    WorkflowStepRun,
)
from apps.automation.workflow_engine import advance_run, enroll, run_due, validate_definition
from apps.contacts.models import Contact
from apps.conversations.models import Conversation


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def account(db):
    from apps.accounts.models import Account
    from apps.billing.models import Plan, Subscription

    acc = Account.objects.create(company_name="TestCo-A")
    plan = Plan.objects.create(
        slug="pa",
        name="PA",
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
def ig_account(account):
    from apps.instagram.models.account import InstagramBusinessAccount

    return InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id="IBA001",
        page_id="PAGE001",
        access_token="test_token",
        verify_token="vtok",
        is_active=True,
    )


@pytest.fixture
def ig_contact_and_contact(account, ig_account):
    from apps.instagram.models.contact import InstagramContact

    contact = Contact.objects.create(account=account, source="instagram")
    ig_contact = InstagramContact.objects.create(
        account=account,
        instagram_scoped_id="IGSID001",
        contact=contact,
    )
    return ig_contact, contact


@pytest.fixture
def ig_conversation(ig_account, ig_contact_and_contact):
    from apps.instagram.models.conversation import InstagramConversation

    ig_contact, _contact = ig_contact_and_contact
    return InstagramConversation.objects.create(
        instagram_account=ig_account,
        instagram_contact=ig_contact,
    )


@pytest.fixture
def spine_ig_conversation(ig_conversation, ig_contact_and_contact):
    """The spine Conversation for an IG thread."""
    return Conversation.get_or_create_for_instagram(ig_conversation)


def _ig_workflow(account, buttons=None, *, on_timeout="stop", next_step="done"):
    buttons = buttons or [{"title": "Yes"}, {"title": "No"}]
    return Workflow.objects.create(
        account=account,
        name="IG-WF",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "conversation.message_received"},
            "steps": [
                {
                    "id": "ask",
                    "type": "send_buttons",
                    "text": "What do you need?",
                    "buttons": buttons,
                    "on_timeout": on_timeout,
                    "next": next_step,
                },
                {"id": "done", "type": "stop"},
                {"id": "timeout_step", "type": "stop"},
                {"id": "stop", "type": "stop"},
            ],
        },
    )


# ── A.1  IG send_buttons parks the run and creates an interaction ─────────────


@pytest.mark.django_db
def test_ig_send_buttons_creates_interaction_and_parks_run(
    account, ig_account, ig_contact_and_contact, ig_conversation, spine_ig_conversation
):
    """IG send_buttons: creates WorkflowInteraction + WAITING run in one transaction."""
    _ig_contact, contact = ig_contact_and_contact
    wf = _ig_workflow(account)

    with patch("apps.instagram.services.outbound.enqueue_reply") as mock_enqueue:
        mock_enqueue.return_value = None  # no real OutboundMessage in test DB

        run = enroll(wf, contact, context={"conversation_id": spine_ig_conversation.public_id})

    assert run is not None
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING

    interaction = WorkflowInteraction.objects.get(run=run, step_id="ask")
    assert interaction.status == WorkflowInteraction.Status.OPEN
    assert set(interaction.expected["option_ids"]) == {"yes", "no"}
    assert interaction.deadline_at is not None

    # The step run is recorded.
    assert WorkflowStepRun.objects.filter(run=run, step_id="ask").exists()

    # enqueue_reply was called with quick_replies containing the token.
    mock_enqueue.assert_called_once()
    qr_payloads = mock_enqueue.call_args.kwargs["quick_replies"]
    tokens_in_payload = [qr["payload"] for qr in qr_payloads]
    assert all(f"{interaction.token}:" in p for p in tokens_in_payload)


# ── A.2  IG inbound tap resumes via claim_reply ───────────────────────────────


@pytest.mark.django_db
def test_ig_tap_resumes_run_via_claim_reply(
    account, ig_account, ig_contact_and_contact, ig_conversation, spine_ig_conversation
):
    """A quick_reply tap with the correct token resumes the parked run."""
    _ig_contact, contact = ig_contact_and_contact
    wf = _ig_workflow(account, next_step="done")

    with patch("apps.instagram.services.outbound.enqueue_reply") as mock_enqueue:
        mock_enqueue.return_value = None
        run = enroll(wf, contact, context={"conversation_id": spine_ig_conversation.public_id})

    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING

    interaction = WorkflowInteraction.objects.get(run=run)
    token = interaction.token
    option_id = interaction.expected["option_ids"][0]

    from apps.automation.interaction import claim_reply

    result = claim_reply(
        conversation=spine_ig_conversation,
        message_key="instagram:IBA001:mid.001",
        reply_id=f"{token}:{option_id}",
        body=option_id,
    )

    assert result == "claimed"
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    interaction.refresh_from_db()
    assert interaction.status == WorkflowInteraction.Status.ANSWERED


# ── A.3  IG timeout takes the on_timeout path ─────────────────────────────────


@pytest.mark.django_db
def test_ig_timeout_takes_on_timeout_path(
    account, ig_account, ig_contact_and_contact, ig_conversation, spine_ig_conversation
):
    """When the IG WAITING run's deadline passes, run_due advances it via on_timeout."""
    _ig_contact, contact = ig_contact_and_contact
    wf = _ig_workflow(account, on_timeout="timeout_step")

    with patch("apps.instagram.services.outbound.enqueue_reply") as mock_enqueue:
        mock_enqueue.return_value = None
        run = enroll(wf, contact, context={"conversation_id": spine_ig_conversation.public_id})

    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING

    WorkflowRun.objects.filter(pk=run.pk).update(
        next_due_at=timezone.now() - timedelta(seconds=1)
    )

    count = run_due()
    assert count >= 1

    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED

    interaction = WorkflowInteraction.objects.get(run=run)
    assert interaction.status == WorkflowInteraction.Status.CANCELLED


# ── A.4  IG outbound failure cancels interaction and fails run ────────────────


@pytest.mark.django_db
def test_ig_outbound_failure_cancels_interaction_and_fails_run(
    account, ig_account, ig_contact_and_contact, ig_conversation, spine_ig_conversation
):
    """mark_outbound_message_failed cancels the open interaction and fails the run."""
    from apps.automation.integrations.instagram import mark_outbound_message_failed
    from apps.instagram.models.message import OutboundMessage

    _ig_contact, contact = ig_contact_and_contact
    wf = _ig_workflow(account)

    import secrets as _secrets

    with patch("apps.instagram.services.outbound.enqueue_reply") as mock_enqueue:
        mock_enqueue.return_value = None
        run = enroll(wf, contact, context={"conversation_id": spine_ig_conversation.public_id})

    run.refresh_from_db()
    step_run = WorkflowStepRun.objects.get(run=run, step_id="ask")

    ig_msg = OutboundMessage.objects.create(
        instagram_account=ig_account,
        idempotency_key=_secrets.token_hex(8),
        recipient_igsid="IGSID001",
        action_type=OutboundMessage.ActionType.DM_REPLY,
        body="What do you need?",
        status=OutboundMessage.Status.FAILED,
        last_error="send failed",
    )
    step_run.ig_outbound_message = ig_msg
    step_run.save(update_fields=["ig_outbound_message"])

    mark_outbound_message_failed(ig_msg)

    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED

    interaction = WorkflowInteraction.objects.get(run=run)
    assert interaction.status == WorkflowInteraction.Status.CANCELLED


# ── A.5  Publish validation rejects send_list for IG trigger ─────────────────


@pytest.mark.django_db
def test_publish_validation_rejects_send_list_for_ig_trigger(account):
    """send_list is not supported on Instagram; the validator must reject it."""
    errors = validate_definition(
        {
            "trigger": {"type": "conversation.message_received"},
            "steps": [
                {
                    "id": "menu",
                    "type": "send_list",
                    "text": "Pick one",
                    "button": "Menu",
                    "rows": [{"id": "a", "title": "A"}],
                },
            ],
        },
        account,
    )
    assert any("Instagram" in (e.get("message") or "") for e in errors), errors


@pytest.mark.django_db
def test_publish_validation_rejects_too_many_buttons(account):
    """More than 13 buttons fails validation."""
    buttons = [{"title": f"Opt {i}"} for i in range(14)]
    errors = validate_definition(
        {
            "trigger": {"type": "conversation.message_received"},
            "steps": [{"id": "ask", "type": "send_buttons", "text": "?", "buttons": buttons}],
        },
        account,
    )
    assert any("buttons" in (e.get("field") or "") for e in errors), errors


@pytest.mark.django_db
def test_publish_validation_rejects_title_too_long(account):
    """A button title longer than 20 chars fails validation."""
    errors = validate_definition(
        {
            "trigger": {"type": "conversation.message_received"},
            "steps": [
                {
                    "id": "ask",
                    "type": "send_buttons",
                    "text": "Pick",
                    "buttons": [{"title": "This title is way too long to fit"}],
                }
            ],
        },
        account,
    )
    assert any("buttons" in (e.get("field") or "") for e in errors), errors


# ── A.6  WA send_buttons still works (no interaction created) ─────────────────


@pytest.mark.django_db
def test_wa_send_buttons_creates_no_interaction(db):
    """WhatsApp send_buttons is unchanged: no WorkflowInteraction row is created."""
    from apps.accounts.models import Account
    from apps.billing.models import Plan, Subscription
    from apps.contacts.models import Contact
    from apps.conversations.models import Conversation
    from apps.whatsapp.models import Conversation as WaConversation
    from apps.whatsapp.models import WhatsAppContact
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

    acc = Account.objects.create(company_name="WA-TestCo")
    plan = Plan.objects.create(
        slug="pwa",
        name="PWA",
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
    pn = WhatsAppBusinessNumber.objects.create(
        account=acc,
        phone_number_id="PNID_WA",
        access_token="tok",
        waba_id="WABA",
        registration_status=WhatsAppBusinessNumber.RegistrationStatus.REGISTERED,
    )
    contact = Contact.objects.create(account=acc, phone="+260971234567")
    wa_contact = WhatsAppContact.objects.create(
        account=acc,
        phone_number="+260971234567",
        contact=contact,
    )
    wa_conv = WaConversation.objects.create(
        account=acc,
        contact=wa_contact,
    )
    spine = Conversation.get_or_create_for_whatsapp(wa_conv)

    wf = Workflow.objects.create(
        account=acc,
        name="WA-WF",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "conversation.message_received"},
            "steps": [
                {
                    "id": "ask",
                    "type": "send_buttons",
                    "text": "Pick?",
                    "buttons": [{"title": "Yes"}, {"title": "No"}],
                    "next": "wait",
                },
                {
                    "id": "wait",
                    "type": "wait_for_reply",
                    "timeout_seconds": 3600,
                    "routes": {"yes": "done"},
                },
                {"id": "done", "type": "stop"},
            ],
        },
    )

    # We need a real WA OutboundMessage in the DB for the FK to resolve.
    from apps.whatsapp.models import OutboundMessage as WaOutboundMessage

    wa_outbound = WaOutboundMessage.objects.create(
        account=acc,
        contact=wa_contact,
        payload={"type": "interactive"},
    )

    with patch("apps.whatsapp.api.send_interactive") as mock_send:
        mock_msg = MagicMock()
        mock_msg.id = wa_outbound.id
        mock_msg.created_at = timezone.now()
        mock_send.return_value = mock_msg
        run = enroll(wf, contact, context={"conversation_id": spine.public_id})

    assert run is not None
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING
    # WA path: no WorkflowInteraction (uses wait_for_reply step to park).
    assert WorkflowInteraction.objects.filter(run=run).count() == 0
    mock_send.assert_called_once()

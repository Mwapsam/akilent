from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun, WorkflowWebhookDelivery
from apps.automation.workflow_engine import advance_run, enroll, on_business_event, run_due, validate_definition
from apps.billing.models import Plan, Subscription
from apps.contacts.models import Contact
from apps.contacts.services import record_contact_event
from apps.email.models import EmailDomain, EmailMessage
from apps.whatsapp.models import MessageTemplate, OutboundMessage, WhatsAppContact


@pytest.fixture
def account(db):
    acc = Account.objects.create(company_name="Acme")
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"),
                               max_emails_per_month=1000, email_apis=True, api_rate_per_min=0)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    EmailDomain.objects.create(account=acc, domain="mail.acme.test",
                               status=EmailDomain.Status.VERIFIED)
    return acc


@pytest.fixture
def contact(account):
    return Contact.objects.create(account=account, email="user@example.com", first_name="Ada")


@pytest.fixture
def whatsapp_template(account):
    return MessageTemplate.objects.create(
        account=account, name="Order update", whatsapp_template_name="order_update",
        content="Your order is {{1}}", approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    )


def _wf(account, steps, *, trigger=None, status=Workflow.Status.PUBLISHED):
    return Workflow.objects.create(
        account=account, name="WF", status=status,
        definition={"trigger": trigger or {}, "steps": steps},
    )


@pytest.mark.django_db
def test_linear_send_then_stop_completes(account, contact):
    wf = _wf(account, [
        {"id": "a", "type": "send_email", "from": "hi@mail.acme.test", "subject": "Hi",
         "text": "yo", "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    assert EmailMessage.objects.filter(account=account, to_email=contact.email).count() == 1
    assert list(run.step_runs.values_list("step_id", flat=True)) == ["a", "b"]


@pytest.mark.django_db
def test_wait_parks_then_run_due_resumes(account, contact):
    wf = _wf(account, [
        {"id": "w", "type": "wait", "seconds": 3600, "next": "done"},
        {"id": "done", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.WAITING
    assert run.next_due_at is not None

    # timer not elapsed -> nothing happens
    assert run_due() == 0

    run.next_due_at = timezone.now() - timedelta(seconds=1)
    run.save(update_fields=["next_due_at"])
    assert run_due() == 1
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED


@pytest.mark.django_db
def test_branch_routes_on_attribute(account, contact):
    contact.attributes = {"country": "ZM"}
    contact.save()
    wf = _wf(account, [
        {"id": "check", "type": "branch", "field": "attributes.country",
         "operator": "eq", "value": "ZM", "on_true": "zm", "on_false": "other"},
        {"id": "zm", "type": "set_attribute", "key": "segment", "value": "zambia", "next": "stop"},
        {"id": "other", "type": "set_attribute", "key": "segment", "value": "row", "next": "stop"},
        {"id": "stop", "type": "stop"},
    ])
    enroll(wf, contact)
    contact.refresh_from_db()
    assert contact.attributes["segment"] == "zambia"


@pytest.mark.django_db
def test_business_event_enrols_contact(account, contact):
    wf = _wf(account, [
        {"id": "a", "type": "send_email", "from": "hi@mail.acme.test", "text": "welcome", "next": "s"},
        {"id": "s", "type": "stop"},
    ], trigger={"type": "business_event", "name": "signup.completed"})

    class _Ev:
        account_id = account.id
        contact_id = contact.id
        name = "signup.completed"
        data = {"plan": "pro"}

    on_business_event(_Ev())
    assert WorkflowRun.objects.filter(workflow=wf, contact=contact).exists()
    assert EmailMessage.objects.filter(to_email=contact.email).count() == 1


@pytest.mark.django_db
def test_draft_workflow_does_not_enrol(account, contact):
    wf = _wf(account, [{"id": "s", "type": "stop"}], status=Workflow.Status.DRAFT)
    assert enroll(wf, contact) is None
    assert not WorkflowRun.objects.filter(workflow=wf).exists()


@pytest.mark.django_db
def test_contact_created_trigger_enrolls(account):
    from apps.contacts.services import upsert_contact

    wf = _wf(account, [
        {"id": "a", "type": "set_attribute", "key": "welcomed", "value": True, "next": "b"},
        {"id": "b", "type": "stop"},
    ], trigger={"type": "contact.created"})
    c, _ = upsert_contact(account, "new@acme.com")
    run = WorkflowRun.objects.get(workflow=wf, contact=c)
    assert run.status == WorkflowRun.Status.COMPLETED
    c.refresh_from_db()
    assert c.attributes.get("welcomed") is True


@pytest.mark.django_db
def test_email_opened_trigger_enrolls(account, contact):
    wf = _wf(account, [
        {"id": "a", "type": "set_attribute", "key": "engaged", "value": True, "next": "b"},
        {"id": "b", "type": "stop"},
    ], trigger={"type": "email.opened"})
    record_contact_event(contact, "email.opened")
    run = WorkflowRun.objects.get(workflow=wf, contact=contact)
    assert run.status == WorkflowRun.Status.COMPLETED


@pytest.mark.django_db
def test_send_whatsapp_step_sends_via_shared_path(account, contact, whatsapp_template):
    WhatsAppContact.objects.create(account=account, phone_number="+260971234567")
    contact.attributes = {"phone": "+260971234567"}
    contact.save()

    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "order_update", "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    assert OutboundMessage.objects.filter(
        account=account, contact__phone_number="+260971234567", template=whatsapp_template
    ).count() == 1
    assert list(run.step_runs.values_list("step_id", flat=True)) == ["a", "b"]


@pytest.mark.django_db
def test_send_email_step_with_send_at_schedules_instead_of_sending_now(account, contact):
    from apps.scheduler.models import ScheduledJob

    future = (timezone.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    wf = _wf(account, [
        {"id": "a", "type": "send_email", "from": "hi@mail.acme.test", "subject": "Hi",
         "text": "yo", "send_at": future, "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    # Scheduled, not sent immediately: no EmailMessage yet, a ScheduledJob instead.
    assert EmailMessage.objects.filter(account=account, to_email=contact.email).count() == 0
    assert ScheduledJob.objects.filter(account=account, kind=ScheduledJob.Kind.EMAIL_SINGLE).count() == 1


def test_validate_definition_flags_invalid_send_at():
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "send_email", "from": "x@y.com", "subject": "hi",
                   "send_at": "not-a-date", "next": "b"}, {"id": "b", "type": "stop"}],
    })
    e = next((e for e in errors if e["step_id"] == "a" and e["field"] == "send_at"), None)
    assert e is not None


@pytest.mark.django_db
def test_send_whatsapp_step_with_send_at_sets_outbound_scheduled_at(account, contact, whatsapp_template):
    WhatsAppContact.objects.create(account=account, phone_number="+260971234567")
    contact.attributes = {"phone": "+260971234567"}
    contact.save()

    future = timezone.now() + timedelta(hours=2)
    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "order_update",
         "send_at": future.strftime("%Y-%m-%dT%H:%M:%S"), "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    msg = OutboundMessage.objects.get(account=account, contact__phone_number="+260971234567")
    # Not "now" — the drain task shouldn't pick this up until send_at.
    assert msg.scheduled_at > timezone.now() + timedelta(minutes=30)


@pytest.mark.django_db
def test_send_whatsapp_step_fails_without_phone_attribute(account, contact, whatsapp_template):
    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "order_update", "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED
    assert not OutboundMessage.objects.exists()
    step_run = run.step_runs.get(step_id="a")
    assert step_run.status == "error"
    assert "phone" in step_run.result["error"]
    # re-running a failed run must not retry or double-record the step
    advance_run(run)
    assert run.step_runs.filter(step_id="a").count() == 1


@pytest.mark.django_db
def test_send_whatsapp_step_fails_for_unknown_template(account, contact):
    WhatsAppContact.objects.create(account=account, phone_number="+260971234567")
    contact.attributes = {"phone": "+260971234567"}
    contact.save()

    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "does_not_exist", "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED
    assert not OutboundMessage.objects.exists()


@pytest.mark.django_db
def test_unsupported_step_type_fails_run_instead_of_skipping(account, contact):
    wf = _wf(account, [
        {"id": "a", "type": "send_carrier_pigeon", "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED
    step_run = run.step_runs.get(step_id="a")
    assert step_run.status == "error"
    assert "send_carrier_pigeon" in step_run.result["error"]
    # step "b" never ran — a bad step halts the workflow, it doesn't skip past it
    assert not run.step_runs.filter(step_id="b").exists()


def _err(errors, step_id, field):
    return next((e for e in errors if e["step_id"] == step_id and e["field"] == field), None)


def test_validate_definition_flags_trigger_errors():
    errors = validate_definition({"trigger": {"type": "bogus"}, "steps": [{"id": "a", "type": "stop"}]})
    e = _err(errors, None, "trigger.type")
    assert e is not None and "trigger.type" in e["message"]

    errors = validate_definition({
        "trigger": {"type": "business_event"},
        "steps": [{"id": "a", "type": "stop"}],
    })
    e = _err(errors, None, "trigger.name")
    assert e is not None


def test_validate_definition_flags_send_email_and_send_whatsapp():
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "send_email", "next": "b"}, {"id": "b", "type": "stop"}],
    })
    assert _err(errors, "a", "template") is not None
    assert _err(errors, "a", "from") is not None

    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "send_whatsapp", "next": "b"}, {"id": "b", "type": "stop"}],
    })
    assert _err(errors, "a", "template") is not None


def test_validate_definition_flags_branch_and_dangling_refs():
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "branch"}],
    })
    assert _err(errors, "a", "field") is not None

    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "stop", "next": "nowhere"},
                  {"id": "b", "type": "branch", "field": "x", "on_true": "gone", "on_false": "b"}],
    })
    # "stop" steps aren't followed via `next`, so no dangling-ref error for "a".
    assert _err(errors, "a", "next") is None
    e = _err(errors, "b", "on_true")
    assert e is not None and "gone" in e["message"]


def test_validate_definition_flags_duplicate_and_missing_ids():
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "stop"}, {"id": "a", "type": "stop"}],
    })
    assert _err(errors, "a", "id") is not None

    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"type": "stop"}],
    })
    e = _err(errors, None, "id")
    assert e is not None


@pytest.mark.django_db
def test_advance_is_idempotent(account, contact):
    wf = _wf(account, [
        {"id": "a", "type": "send_email", "from": "hi@mail.acme.test", "text": "x", "next": "w"},
        {"id": "w", "type": "wait", "seconds": 3600, "next": "stop"},
        {"id": "stop", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    # re-run advance while it's parked at the wait — must not re-send
    advance_run(run)
    advance_run(run)
    assert EmailMessage.objects.filter(to_email=contact.email).count() == 1
    assert WorkflowStepRun.objects.filter(run=run, step_id="a").count() == 1


# ── send_whatsapp: production-safety fixes ──────────────────────────────────

@pytest.mark.django_db
def test_send_whatsapp_step_fails_when_template_not_approved(account, contact):
    WhatsAppContact.objects.create(account=account, phone_number="+260971234567")
    contact.attributes = {"phone": "+260971234567"}
    contact.save()
    MessageTemplate.objects.create(
        account=account, name="Pending", whatsapp_template_name="pending_tpl",
        content="Hi", approval_status=MessageTemplate.ApprovalStatus.PENDING,
    )
    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "pending_tpl", "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED
    assert not OutboundMessage.objects.exists()
    step_run = run.step_runs.get(step_id="a")
    assert "not approved" in step_run.result["error"]


@pytest.mark.django_db
def test_send_whatsapp_variable_mapping_resolves_contact_and_context_fields(account, contact):
    WhatsAppContact.objects.create(account=account, phone_number="+260971234567")
    contact.attributes = {"phone": "+260971234567", "first_name": "Ada"}
    contact.save()
    MessageTemplate.objects.create(
        account=account, name="Order confirmation", whatsapp_template_name="order_confirm",
        content="Hi {{1}}, order {{2}}", approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        variables=["name", "order_number"],
    )
    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "order_confirm", "next": "b",
         "variable_mapping": {"name": "contact.first_name", "order_number": "context.order_number"}},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact, context={"order_number": "ORD-123"})
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    msg = OutboundMessage.objects.get(account=account)
    assert msg.payload["params"] == {"name": "Ada", "order_number": "ORD-123"}


@pytest.mark.django_db
def test_send_whatsapp_variable_mapping_does_not_leak_unlisted_context_keys(account, contact):
    """Regression test: only explicitly mapped context keys may reach the outbound message."""
    WhatsAppContact.objects.create(account=account, phone_number="+260971234567")
    contact.attributes = {"phone": "+260971234567"}
    contact.save()
    MessageTemplate.objects.create(
        account=account, name="Order confirmation", whatsapp_template_name="order_confirm",
        content="Order {{1}}", approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        variables=["order_number"],
    )
    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "order_confirm", "next": "b",
         "variable_mapping": {"order_number": "context.order_number"}},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact, context={
        "order_number": "ORD-123",
        "internal_customer_notes": "flagged for fraud review",
        "admin_token": "super-secret",
    })
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    msg = OutboundMessage.objects.get(account=account)
    assert msg.payload["params"] == {"order_number": "ORD-123"}


@pytest.mark.django_db
def test_send_whatsapp_missing_whatsapp_contact_raises_clear_error(account, contact, whatsapp_template):
    contact.attributes = {"phone": "+260971234567"}
    contact.save()
    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "order_update", "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.FAILED
    assert not OutboundMessage.objects.exists()
    step_run = run.step_runs.get(step_id="a")
    assert "no WhatsApp contact" in step_run.result["error"]


@pytest.mark.django_db
def test_send_whatsapp_auto_creates_contact_when_enabled(account, contact, whatsapp_template):
    contact.attributes = {"phone": "+260971234567"}
    contact.save()
    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "order_update", "next": "b",
         "auto_create_contact": True},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    wa_contact = WhatsAppContact.objects.get(account=account, phone_number="+260971234567")
    # Auto-provisioning never implies consent — the send-authorization layer
    # in apps.whatsapp remains the sole authority on whether this is allowed.
    assert wa_contact.opt_in_status == WhatsAppContact.OptInStatus.UNKNOWN


@pytest.mark.django_db
def test_validate_definition_rejects_missing_or_unapproved_template_for_account(account):
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "send_whatsapp", "template": "nope", "next": "b"},
                  {"id": "b", "type": "stop"}],
    }, account=account)
    e = _err(errors, "a", "template")
    assert e is not None and e["severity"] == "error"

    MessageTemplate.objects.create(
        account=account, name="Pending", whatsapp_template_name="pending_tpl",
        content="Hi", approval_status=MessageTemplate.ApprovalStatus.PENDING,
    )
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "send_whatsapp", "template": "pending_tpl", "next": "b"},
                  {"id": "b", "type": "stop"}],
    }, account=account)
    e = _err(errors, "a", "template")
    assert e is not None and e["severity"] == "warning"


@pytest.mark.django_db
def test_validate_definition_requires_variable_mapping_for_account(account):
    MessageTemplate.objects.create(
        account=account, name="Order", whatsapp_template_name="order_tpl",
        content="Hi {{1}}", approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        variables=["name"],
    )
    errors = validate_definition({
        "trigger": {"type": "manual"},
        "steps": [{"id": "a", "type": "send_whatsapp", "template": "order_tpl", "next": "b"},
                  {"id": "b", "type": "stop"}],
    }, account=account)
    e = _err(errors, "a", "variable_mapping")
    assert e is not None and "name" in e["message"]


@pytest.mark.django_db
def test_workflow_step_run_reconciles_to_failed_on_outbound_permanent_failure(account, contact, whatsapp_template):
    from apps.automation.integrations.whatsapp import mark_outbound_message_failed

    WhatsAppContact.objects.create(account=account, phone_number="+260971234567")
    contact.attributes = {"phone": "+260971234567"}
    contact.save()
    wf = _wf(account, [
        {"id": "a", "type": "send_whatsapp", "template": "order_update", "next": "b"},
        {"id": "b", "type": "stop"},
    ])
    run = enroll(wf, contact)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED

    msg = OutboundMessage.objects.get(account=account)
    msg.mark_failed("permanently rejected by Meta", terminal=True)

    mark_outbound_message_failed(msg)
    run.refresh_from_db()
    step_run = run.step_runs.get(step_id="a")
    assert step_run.status == "failed"
    assert run.status == WorkflowRun.Status.FAILED

    # idempotent: calling again must not error or change anything further
    mark_outbound_message_failed(msg)
    step_run.refresh_from_db()
    run.refresh_from_db()
    assert step_run.status == "failed"
    assert run.status == WorkflowRun.Status.FAILED

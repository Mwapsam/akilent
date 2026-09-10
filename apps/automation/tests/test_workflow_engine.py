from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun
from apps.automation.workflow_engine import advance_run, enroll, on_business_event, run_due
from apps.billing.models import Plan, Subscription
from apps.contacts.models import Contact
from apps.contacts.services import record_contact_event
from apps.email.models import EmailDomain, EmailMessage


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

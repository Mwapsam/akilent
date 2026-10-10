"""Workstream C — Custom attributes contract.

Covers:
- set_attributes: basic write, type coercion, partial update, None removal.
- set_attributes: rejects unknown keys, archived defs.
- set_attributes: emits attribute.changed only on actual change.
- set_attributes: works on Lead and Deal (not just Contact).
- archive_attribute_def: blocked while a published workflow references the key.
- archive_attribute_def: blocked while an active/waiting run references the key.
- archive_attribute_def: succeeds when nothing references the key.
- _apply_set_attribute in the workflow engine goes through set_attributes.
"""

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.contacts.attributes import archive_attribute_def, set_attributes
from apps.contacts.models import CustomAttributeDef


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def account(db):
    from apps.accounts.models import Account

    return Account.objects.create(company_name="TestCo-C")


@pytest.fixture
def contact(account):
    from apps.contacts.models import Contact

    return Contact.objects.create(account=account, source="test")


@pytest.fixture
def lead(account, contact):
    from apps.crm.models import Lead

    return Lead.objects.create(account=account, contact=contact)


@pytest.fixture
def deal(account, contact):
    from apps.crm.models import Deal, Pipeline, Stage

    pipeline = Pipeline.objects.create(account=account, name="Main")
    stage = Stage.objects.create(pipeline=pipeline, name="Open", order=0)
    return Deal.objects.create(
        account=account,
        contact=contact,
        pipeline=pipeline,
        stage=stage,
        title="Test deal",
    )


def _def(account, key, type_="string", entity="contact", **kw):
    return CustomAttributeDef.objects.create(
        account=account, entity=entity, key=key, type=type_, **kw
    )


# ── C.1  Basic write and event ────────────────────────────────────────────────


@pytest.mark.django_db
def test_set_attributes_writes_contact(account, contact):
    _def(account, "industry")
    diff = set_attributes(contact, {"industry": "Tech"}, source="test")
    assert diff == {"industry": (None, "Tech")}
    contact.refresh_from_db()
    assert contact.attributes["industry"] == "Tech"


@pytest.mark.django_db
def test_set_attributes_emits_event(account, contact):
    from apps.conversations.models import Event

    _def(account, "plan")
    set_attributes(contact, {"plan": "pro"}, actor="user_1", source="api")
    ev = Event.objects.filter(account=account, type="attribute.changed").first()
    assert ev is not None
    assert ev.payload["key"] == "plan"
    assert ev.payload["new_value"] == "pro"
    assert ev.payload["actor"] == "user_1"


@pytest.mark.django_db
def test_set_attributes_no_event_when_no_change(account, contact):
    from apps.conversations.models import Event

    _def(account, "color")
    contact.attributes = {"color": "blue"}
    contact.save()
    diff = set_attributes(contact, {"color": "blue"}, source="test")
    assert diff == {}
    assert Event.objects.filter(account=account, type="attribute.changed").count() == 0


# ── C.2  Partial updates and removal ─────────────────────────────────────────


@pytest.mark.django_db
def test_set_attributes_partial_update(account, contact):
    _def(account, "a")
    _def(account, "b")
    contact.attributes = {"a": "old_a", "b": "old_b"}
    contact.save()
    # Only update "a"; "b" must be unchanged.
    diff = set_attributes(contact, {"a": "new_a"}, source="test")
    assert diff == {"a": ("old_a", "new_a")}
    contact.refresh_from_db()
    assert contact.attributes == {"a": "new_a", "b": "old_b"}


@pytest.mark.django_db
def test_set_attributes_none_removes_key(account, contact):
    _def(account, "notes")
    contact.attributes = {"notes": "hello"}
    contact.save()
    diff = set_attributes(contact, {"notes": None}, source="test")
    assert diff == {"notes": ("hello", None)}
    contact.refresh_from_db()
    assert "notes" not in contact.attributes


# ── C.3  Type coercion ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_coerce_number(account, contact):
    _def(account, "budget", type_="number")
    set_attributes(contact, {"budget": 1250}, source="test")
    contact.refresh_from_db()
    assert contact.attributes["budget"] == "1250.00"


@pytest.mark.django_db
def test_coerce_boolean(account, contact):
    _def(account, "verified", type_="boolean")
    set_attributes(contact, {"verified": "true"}, source="test")
    contact.refresh_from_db()
    assert contact.attributes["verified"] is True


@pytest.mark.django_db
def test_coerce_date(account, contact):
    import datetime

    _def(account, "joined", type_="date")
    set_attributes(contact, {"joined": datetime.date(2026, 1, 15)}, source="test")
    contact.refresh_from_db()
    assert contact.attributes["joined"] == "2026-01-15"


@pytest.mark.django_db
def test_coerce_choice_valid(account, contact):
    _def(account, "tier", type_="choice", options=[{"key": "gold"}, {"key": "silver"}])
    set_attributes(contact, {"tier": "gold"}, source="test")
    contact.refresh_from_db()
    assert contact.attributes["tier"] == "gold"


@pytest.mark.django_db
def test_coerce_choice_invalid_raises(account, contact):
    _def(account, "tier", type_="choice", options=[{"key": "gold"}, {"key": "silver"}])
    with pytest.raises(ValueError, match="not a valid option"):
        set_attributes(contact, {"tier": "platinum"}, source="test")


# ── C.4  Validation errors ────────────────────────────────────────────────────


@pytest.mark.django_db
def test_rejects_unknown_key(account, contact):
    with pytest.raises(ValueError, match="Unknown attribute key"):
        set_attributes(contact, {"ghost": "value"}, source="test")


@pytest.mark.django_db
def test_rejects_archived_def(account, contact):
    defn = _def(account, "old_field")
    defn.archived_at = timezone.now()
    defn.save()
    with pytest.raises(ValueError, match="archived"):
        set_attributes(contact, {"old_field": "x"}, source="test")


# ── C.5  Lead and Deal ────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_set_attributes_on_lead(account, lead):
    _def(account, "budget", entity="lead", type_="number")
    set_attributes(lead, {"budget": "500"}, source="test")
    lead.refresh_from_db()
    assert lead.attributes["budget"] == "500.00"


@pytest.mark.django_db
def test_set_attributes_on_deal(account, deal):
    _def(account, "contract_type", entity="deal")
    set_attributes(deal, {"contract_type": "annual"}, source="test")
    deal.refresh_from_db()
    assert deal.attributes["contract_type"] == "annual"


# ── C.6  Cross-entity isolation ───────────────────────────────────────────────


@pytest.mark.django_db
def test_contact_key_not_visible_to_lead(account, lead):
    """A contact-entity def must not match a lead write."""
    _def(account, "budget", entity="contact")
    with pytest.raises(ValueError, match="Unknown attribute key"):
        set_attributes(lead, {"budget": "100"}, source="test")


# ── C.7  Archive lifecycle ────────────────────────────────────────────────────


@pytest.mark.django_db
def test_archive_succeeds_when_no_references(account):
    defn = _def(account, "old_note")
    archive_attribute_def(defn)
    defn.refresh_from_db()
    assert defn.is_archived


@pytest.mark.django_db
def test_archive_blocked_by_published_workflow(account):
    from apps.automation.models import Workflow

    defn = _def(account, "score")
    Workflow.objects.create(
        account=account,
        name="Score WF",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "contact.created"},
            "steps": [
                {
                    "id": "s1",
                    "type": "set_attribute",
                    "key": "score",
                    "value": "10",
                    "target": "contact",
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="published workflow"):
        archive_attribute_def(defn)


@pytest.mark.django_db
def test_archive_blocked_by_active_run(account, contact):
    from apps.automation.models import Workflow, WorkflowRun

    defn = _def(account, "stage_attr")
    # Use DRAFT so the published-workflow check doesn't fire first.
    wf = Workflow.objects.create(
        account=account,
        name="Stage WF",
        status=Workflow.Status.DRAFT,
        definition={
            "trigger": {"type": "contact.created"},
            "steps": [
                {
                    "id": "s1",
                    "type": "set_attribute",
                    "key": "stage_attr",
                    "value": "x",
                    "target": "contact",
                }
            ],
        },
    )
    WorkflowRun.objects.create(
        workflow=wf,
        contact=contact,
        status=WorkflowRun.Status.ACTIVE,
        current_step="s1",
    )
    with pytest.raises(ValueError, match="active/waiting"):
        archive_attribute_def(defn)


@pytest.mark.django_db
def test_archive_already_archived_raises(account):
    defn = _def(account, "stale")
    defn.archived_at = timezone.now()
    defn.save()
    with pytest.raises(ValueError, match="already archived"):
        archive_attribute_def(defn)


# ── C.8  workflow_engine._apply_set_attribute compatibility and service path ──


@pytest.mark.django_db
def test_workflow_engine_set_attribute_compat_fallback(account, contact):
    """No def → compatibility fallback: run completes, attribute written directly."""
    from apps.automation.models import Workflow, WorkflowRun
    from apps.automation.workflow_engine import advance_run

    # No def created → legacy direct write, run still completes.
    wf = Workflow.objects.create(
        account=account,
        name="Compat WF",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "contact.created"},
            "steps": [
                {"id": "s1", "type": "set_attribute", "key": "legacy_key", "value": "v"},
                {"id": "done", "type": "stop"},
            ],
        },
    )
    run = WorkflowRun.objects.create(
        workflow=wf,
        contact=contact,
        status=WorkflowRun.Status.ACTIVE,
        current_step="s1",
    )
    advance_run(run)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    contact.refresh_from_db()
    assert contact.attributes.get("legacy_key") == "v"


@pytest.mark.django_db
def test_workflow_engine_set_attribute_uses_service_when_def_exists(account, contact):
    """When a def exists, set_attributes is used (type coercion applies)."""
    from apps.automation.models import Workflow, WorkflowRun
    from apps.automation.workflow_engine import advance_run

    _def(account, "score", type_="number")
    wf = Workflow.objects.create(
        account=account,
        name="Score WF",
        status=Workflow.Status.PUBLISHED,
        definition={
            "trigger": {"type": "contact.created"},
            "steps": [
                {"id": "s1", "type": "set_attribute", "key": "score", "value": "99"},
                {"id": "done", "type": "stop"},
            ],
        },
    )
    run = WorkflowRun.objects.create(
        workflow=wf,
        contact=contact,
        status=WorkflowRun.Status.ACTIVE,
        current_step="s1",
    )
    advance_run(run)
    run.refresh_from_db()
    assert run.status == WorkflowRun.Status.COMPLETED
    contact.refresh_from_db()
    # Number type → coerced to decimal string "99.00"
    assert contact.attributes.get("score") == "99.00"


# ── C.9  Event rollback ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_event_rollback_on_emit_failure(account, contact):
    """If emit_event raises inside the transaction, neither the attribute nor
    the event row persists (full transaction rollback)."""
    from unittest.mock import patch

    from apps.conversations.models import Event

    _def(account, "rollback_key")
    contact.attributes = {}
    contact.save()

    with patch(
        "apps.contacts.attributes._emit_changed",
        side_effect=RuntimeError("emit failed"),
    ):
        with pytest.raises(RuntimeError, match="emit failed"):
            set_attributes(contact, {"rollback_key": "bad"}, source="test")

    contact.refresh_from_db()
    # The attribute write rolled back.
    assert "rollback_key" not in contact.attributes
    assert Event.objects.filter(account=account, type="attribute.changed").count() == 0


# ── C.10  Archive race: archived def detected at execution time ───────────────


@pytest.mark.django_db
def test_set_attributes_detects_archived_def_at_execution(account, contact):
    """If a def is archived between validation and write (simulated by archiving
    before the call), set_attributes rejects the write — the lock re-checks the
    live row, so archive → write races always yield a rejection."""
    defn = _def(account, "live_key")
    contact.attributes = {}
    contact.save()

    # Archive after creating but before writing — simulates the race.
    defn.archived_at = timezone.now()
    defn.save()

    with pytest.raises(ValueError, match="archived"):
        set_attributes(contact, {"live_key": "val"}, source="test")

    contact.refresh_from_db()
    assert "live_key" not in contact.attributes

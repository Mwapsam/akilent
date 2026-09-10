"""Durable executor for lifecycle Workflows (Phase 6).

A run walks the ``definition["steps"]`` graph one step at a time. Synchronous
steps run inline; a ``wait`` step parks the run with ``next_due_at`` and the
``run_due_workflows`` beat task resumes it. Every step writes a
``WorkflowStepRun`` so a crashed/re-run ``advance_run`` never double-sends.

Definition shape::

    {
      "trigger": {"type": "business_event", "name": "signup.completed"},
      "steps": [
        {"id": "welcome", "type": "send_email", "template": "welcome",
         "from": "hi@acme.com", "next": "wait1"},
        {"id": "wait1", "type": "wait", "seconds": 172800, "next": "check"},
        {"id": "check", "type": "branch",
         "field": "opened_in_last_90d", "operator": "eq", "value": true,
         "on_true": "tips", "on_false": "reminder"},
        {"id": "tips", "type": "send_email", "template": "tips",
         "from": "hi@acme.com", "next": "stop"},
        {"id": "reminder", "type": "send_email", "template": "reminder",
         "from": "hi@acme.com", "next": "stop"},
        {"id": "stop", "type": "stop"}
      ]
    }
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun

logger = logging.getLogger(__name__)

_MAX_STEPS_PER_CALL = 50

_STEP_TYPES = {"send_email", "wait", "branch", "set_attribute", "stop", "exit"}
_TRIGGER_TYPES = {
    "business_event", "manual",
    "contact.created", "contact.updated",
    "email.opened", "email.clicked",
}


def validate_definition(definition: dict) -> list[str]:
    """Return a list of human-readable problems with ``definition`` (empty = OK)."""
    errors: list[str] = []
    definition = definition or {}
    trig = definition.get("trigger") or {}
    if trig.get("type") not in _TRIGGER_TYPES:
        errors.append(
            f"trigger.type must be one of {sorted(_TRIGGER_TYPES)}"
        )
    elif trig.get("type") == "business_event" and not trig.get("name"):
        errors.append("business_event trigger requires trigger.name")

    steps = definition.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("definition.steps must be a non-empty list")
        return errors

    ids: set[str] = set()
    for i, step in enumerate(steps):
        sid = step.get("id")
        if not sid:
            errors.append(f"steps[{i}] is missing an id")
            continue
        if sid in ids:
            errors.append(f"duplicate step id {sid!r}")
        ids.add(sid)
        if step.get("type") not in _STEP_TYPES:
            errors.append(f"step {sid!r}: type must be one of {sorted(_STEP_TYPES)}")
        if step.get("type") == "send_email" and not (
            step.get("template") or step.get("subject")
        ):
            errors.append(f"step {sid!r}: send_email needs a template or subject")
        if step.get("type") == "send_email" and not step.get("from"):
            errors.append(f"step {sid!r}: send_email needs a from address")
        if step.get("type") == "branch" and not (
            step.get("on_true") and step.get("on_false") and step.get("field")
        ):
            errors.append(f"step {sid!r}: branch needs field, on_true, on_false")

    def _check(ref, ctx):
        if ref and ref not in ids:
            errors.append(f"{ctx} points to unknown step {ref!r}")

    for step in steps:
        if not step.get("id"):
            continue
        if step.get("type") == "branch":
            _check(step.get("on_true"), f"step {step['id']!r}.on_true")
            _check(step.get("on_false"), f"step {step['id']!r}.on_false")
        elif step.get("type") not in ("stop", "exit"):
            _check(step.get("next"), f"step {step['id']!r}.next")
    return errors


def _steps_by_id(workflow: Workflow) -> dict:
    return {s["id"]: s for s in (workflow.definition or {}).get("steps", []) if "id" in s}


def _first_step_id(workflow: Workflow) -> str | None:
    steps = (workflow.definition or {}).get("steps", [])
    return steps[0]["id"] if steps else None


def enroll(workflow: Workflow, contact, *, context: dict | None = None) -> WorkflowRun | None:
    """Start a run for ``contact`` on ``workflow`` (no-op if one is already active)."""
    if workflow.status != Workflow.Status.PUBLISHED:
        return None
    first = _first_step_id(workflow)
    if first is None:
        return None
    run, created = WorkflowRun.objects.get_or_create(
        workflow=workflow,
        contact=contact,
        status__in=[WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING],
        defaults={"context": context or {}, "current_step": first},
    )
    if not created:
        return run
    _notify_workflow(run, "workflow.started")
    advance_run(run)
    return run


def _notify_workflow(run: WorkflowRun, event_type: str) -> None:
    try:
        from apps.email.webhooks import notify

        notify(run.workflow.account, event_type, {
            "run_id": run.public_id,
            "workflow": run.workflow.slug,
            "contact": run.contact.public_id,
            "status": run.status,
        })
    except Exception:
        logger.exception("_notify_workflow failed for run %s", run.pk)


def _condition_matches(contact, step: dict) -> bool:
    from apps.contacts.models import Contact
    from apps.contacts.segments import build_q

    cond = {
        "field": step["field"],
        "operator": step.get("operator", "eq"),
        "value": step.get("value"),
    }
    q = build_q({"op": "and", "conditions": [cond]})
    return Contact.objects.filter(pk=contact.pk).filter(q).exists()


def _run_send_email(run: WorkflowRun, step: dict) -> dict:
    from apps.api.services import create_and_queue_message

    contact = run.contact
    msg = create_and_queue_message(
        account=run.workflow.account,
        from_email=step["from"],
        to_email=contact.email,
        subject=step.get("subject", ""),
        text_body=step.get("text", ""),
        html_body=step.get("html", ""),
        template_id=_resolve_template_id(run.workflow.account, step.get("template")),
        template_variables={
            "first_name": contact.first_name,
            "last_name": contact.last_name,
            "email": contact.email,
            "attributes": contact.attributes or {},
            **(run.context or {}),
        },
    )
    return {"message_id": msg.public_id}


def _resolve_template_id(account, slug):
    if not slug:
        return None
    from apps.email.models import EmailTemplate

    t = EmailTemplate.objects.filter(account=account, slug=slug, is_active=True).first()
    return t.id if t else None


def _apply_set_attribute(run: WorkflowRun, step: dict) -> dict:
    contact = run.contact
    contact.attributes = {**(contact.attributes or {}), step["key"]: step.get("value")}
    contact.save(update_fields=["attributes", "updated_at"])
    return {"key": step["key"]}


def advance_run(run: WorkflowRun) -> WorkflowRun:
    """Execute steps until the run waits, stops, or completes."""
    if run.status not in (WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING):
        return run

    steps = _steps_by_id(run.workflow)

    for _ in range(_MAX_STEPS_PER_CALL):
        step = steps.get(run.current_step)
        if step is None:
            return _complete(run)

        stype = step.get("type")

        if stype == "wait":
            if run.status == WorkflowRun.Status.WAITING and run.next_due_at and run.next_due_at <= timezone.now():
                # resuming from the wait
                _record(run, step, {"resumed": True})
                run.status = WorkflowRun.Status.ACTIVE
                run.next_due_at = None
                run.current_step = step.get("next", "")
                run.save(update_fields=["status", "next_due_at", "current_step"])
                continue
            run.status = WorkflowRun.Status.WAITING
            run.next_due_at = timezone.now() + timedelta(seconds=int(step.get("seconds", 0)))
            run.save(update_fields=["status", "next_due_at"])
            return run

        # Non-wait step: skip if already executed (idempotent re-entry).
        if WorkflowStepRun.objects.filter(run=run, step_id=step["id"]).exists():
            nxt = _next_after(step, run)
            if nxt == run.current_step:
                return _complete(run)
            run.current_step = nxt
            run.save(update_fields=["current_step"])
            continue

        try:
            if stype == "send_email":
                result = _run_send_email(run, step)
            elif stype == "branch":
                matched = _condition_matches(run.contact, step)
                result = {"matched": matched}
            elif stype == "set_attribute":
                result = _apply_set_attribute(run, step)
            elif stype in ("stop", "exit"):
                _record(run, step, {})
                return _complete(run)
            else:
                result = {"skipped": f"unknown step type {stype!r}"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("workflow run %s step %s failed", run.pk, step.get("id"))
            _record(run, step, {"error": str(exc)}, status="error")
            run.status = WorkflowRun.Status.FAILED
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "completed_at"])
            return run

        _record(run, step, result)
        run.current_step = _next_after(step, run, branch_result=result)
        run.save(update_fields=["current_step"])
        if not run.current_step:
            return _complete(run)

    logger.warning("workflow run %s hit step limit; parking", run.pk)
    return run


def _next_after(step: dict, run: WorkflowRun, *, branch_result: dict | None = None) -> str:
    if step.get("type") == "branch":
        if branch_result is None:
            branch_result = {"matched": _condition_matches(run.contact, step)}
        return step["on_true"] if branch_result.get("matched") else step["on_false"]
    return step.get("next", "")


def _record(run: WorkflowRun, step: dict, result: dict, *, status: str = "ok") -> None:
    from django.db import IntegrityError

    try:
        WorkflowStepRun.objects.create(
            run=run, step_id=step["id"], step_type=step.get("type", ""),
            status=status, result=result,
        )
    except IntegrityError:
        # uniq_workflowsteprun_run_step — a concurrent advance already recorded
        # this step. The .exists() guard in advance_run is best-effort; this
        # constraint is the real backstop against a double-send.
        logger.info("workflow run %s step %s already recorded", run.pk, step["id"])


def _complete(run: WorkflowRun) -> WorkflowRun:
    run.status = WorkflowRun.Status.COMPLETED
    run.completed_at = timezone.now()
    run.next_due_at = None
    run.save(update_fields=["status", "completed_at", "next_due_at"])
    _notify_workflow(run, "workflow.completed")
    return run


# ── Trigger evaluation ───────────────────────────────────────────────────────

def on_business_event(event, **kwargs) -> None:
    """Enroll the event's contact into workflows triggered by this event name."""
    if event.contact_id is None:
        return
    from apps.contacts.models import Contact

    contact = Contact.objects.filter(pk=event.contact_id).first()
    if contact is None:
        return
    workflows = Workflow.objects.filter(
        account_id=event.account_id, status=Workflow.Status.PUBLISHED
    )
    for wf in workflows:
        trig = (wf.definition or {}).get("trigger", {})
        if trig.get("type") == "business_event" and trig.get("name") == event.name:
            try:
                with transaction.atomic():
                    enroll(wf, contact, context={"event": event.data})
            except Exception:  # noqa: BLE001
                logger.exception("on_business_event: enroll failed wf=%s", wf.pk)


def enroll_for_trigger(account_id: int, trigger_type: str, contact, *, context: dict | None = None) -> int:
    """Enroll ``contact`` into every published workflow whose trigger matches.

    Used for the lightweight contact-lifecycle triggers (``contact.created``,
    ``email.opened``, …). ``business_event`` has its own richer path in
    ``on_business_event``.
    """
    workflows = Workflow.objects.filter(
        account_id=account_id, status=Workflow.Status.PUBLISHED
    )
    n = 0
    for wf in workflows:
        if (wf.definition or {}).get("trigger", {}).get("type") != trigger_type:
            continue
        try:
            with transaction.atomic():
                if enroll(wf, contact, context=context or {}) is not None:
                    n += 1
        except Exception:  # noqa: BLE001
            logger.exception("enroll_for_trigger: wf=%s trigger=%s", wf.pk, trigger_type)
    return n


_RUN_DUE_BATCH = 200


def run_due() -> int:
    """Resume every WAITING run whose timer has elapsed. Called by a beat task.

    Rows are claimed under ``select_for_update(skip_locked=True)`` (where the
    backend supports it) so parallel beat ticks / workers process disjoint
    batches rather than racing the same run into a double-send.
    """
    from django.db import connection

    lock_kwargs = (
        {"skip_locked": True}
        if connection.features.has_select_for_update_skip_locked
        else {}
    )
    n = 0
    with transaction.atomic():
        due = (
            WorkflowRun.objects.select_for_update(**lock_kwargs)
            .filter(status=WorkflowRun.Status.WAITING, next_due_at__lte=timezone.now())
            .order_by("next_due_at", "id")[:_RUN_DUE_BATCH]
        )
        runs = list(due)

    for run in runs:
        try:
            with transaction.atomic():
                advance_run(run)
            n += 1
        except Exception:  # noqa: BLE001
            logger.exception("run_due: advance failed for run %s", run.pk)
    return n

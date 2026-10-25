"""Public API for the automation module.

This is the single interface for rule evaluation and execution.
The implementation of these functions can change internally without affecting call sites.

Direct imports of apps.automation.* are not allowed outside of automation.
Direct imports of automation rules/workflows should go through this API.
"""

import logging

from apps.accounts.models import Account

logger = logging.getLogger(__name__)


def get_matching_rules(account: Account, trigger_event: str):
    """Get AutomationRule instances matching a trigger event for an account.

    Args:
        account: The account to query rules for
        trigger_event: The trigger event type (e.g. 'message_received')

    Returns:
        QuerySet of matching AutomationRule instances
    """
    from apps.automation.models import AutomationRule

    return AutomationRule.objects.filter(
        account=account,
        trigger_event=trigger_event,
        is_active=True,
    )


def count_active_rules(account: Account) -> int:
    """Automations that are on: published workflows, plus any active legacy rules.

    Used by billing for the plan's ``automation_rules`` limit.
    """
    from apps.automation.models import AutomationRule, Workflow

    return (
        Workflow.objects.filter(
            account=account, status=Workflow.Status.PUBLISHED
        ).count()
        + AutomationRule.objects.filter(account=account, is_active=True).count()
    )


class AutomationLimitReached(ValueError):
    """Turning one more automation on would pass the plan's limit. The message is for the owner."""


def ensure_room_to_turn_on(account, workflow=None) -> None:
    """Raise AutomationLimitReached if turning ``workflow`` (or a new one) on would pass the
    plan's active-automations limit. An automation that's already on is always fine."""
    from apps.automation.models import Workflow
    from apps.billing import api as billing_api

    if (
        workflow is not None
        and workflow.pk
        and workflow.status == Workflow.Status.PUBLISHED
    ):
        return
    try:
        billing_api.require_room(account, "automation_rules")
    except billing_api.LimitReached as exc:
        raise AutomationLimitReached(
            f"{exc} Turn another automation off first, or upgrade your plan."
        ) from exc


def evaluate_conditions(rule, context: dict) -> bool:
    """Evaluate whether a rule's conditions match the given context.

    Args:
        rule: An AutomationRule instance
        context: Context dict with message/event data

    Returns:
        True if all conditions are met, False otherwise
    """
    from apps.automation.rules import evaluate_conditions as _evaluate

    return _evaluate(rule, context)


def execute_rule(rule, context: dict) -> dict:
    """Execute a rule's actions for the given context.

    This is called after conditions have been verified. The rule's actions
    are executed, and the result is returned. If execution fails, an exception
    is logged and re-raised (caller responsibility to handle).

    Args:
        rule: An AutomationRule instance
        context: Context dict with message/event data

    Returns:
        dict with execution result info (implementation-dependent)

    Raises:
        Any exception raised by the rule action execution
    """
    from apps.automation.workflows import execute_rule as _execute

    return _execute(rule, context)  # type: ignore[func-returns-value,return-value]


def upsert_published_workflow(account, *, slug: str, name: str, definition: dict):
    """Create or update a published Workflow by slug — idempotent by design.

    Used by ``apps.verticals`` to install a vertical Starter pack's bundled
    workflows without importing ``apps.automation.models`` directly (module
    boundary rule: cross-app model access goes through this public API).
    """
    from django.db import transaction

    from apps.automation.models import Workflow
    from apps.contacts.attributes import verify_defs_for_publish

    ensure_room_to_turn_on(
        account, Workflow.objects.filter(account=account, slug=slug).first()
    )
    with transaction.atomic():
        verify_defs_for_publish(account, definition)
        workflow, _ = Workflow.objects.update_or_create(
            account=account,
            slug=slug,
            defaults={
                "name": name,
                "definition": definition,
                "status": Workflow.Status.PUBLISHED,
            },
        )
    return workflow


class WorkflowNotReady(ValueError):
    """The workflow has errors that must be fixed before it can be turned on."""


def live_definition_problems(account, definition: dict) -> list[dict]:
    """Blocking problems that stop ``definition`` from running live, or ``[]``.

    The one check every path that turns a workflow on, or changes one that is already on, must
    pass: the editor's Save, Publish, the public API's PATCH and publish. Warnings are left out
    (they never block). Runs ``verify_defs_for_publish`` in its own transaction so a missing or
    archived custom field is reported the same way as any other error.
    """
    from django.db import transaction

    from apps.automation.workflow_engine import validate_definition
    from apps.contacts.attributes import verify_defs_for_publish

    problems = [
        e
        for e in validate_definition(definition, account=account)
        if e.get("severity", "error") != "warning"
    ]
    if problems:
        return problems
    try:
        with transaction.atomic():
            verify_defs_for_publish(account, definition)
    except ValueError as exc:
        return [{"message": str(exc), "severity": "error"}]
    return []


def unique_workflow_slug(account, name: str) -> str:
    """A slug for a new workflow called ``name`` that no other workflow of ``account`` uses."""
    from django.utils.text import slugify

    from apps.automation.models import Workflow

    base = (slugify(name) or "workflow")[:150]
    slug, n = base, 2
    while Workflow.objects.filter(account=account, slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def save_built_workflow(
    account, *, slug: str, name: str, definition: dict, turn_on: bool = False
):
    """Save a drafted workflow (from Build: a recommendation, an AI request, a repeated reply).

    ``turn_on=False`` saves a DRAFT and never touches a live automation: if one with this slug is
    already on, the draft gets its own slug. ``turn_on=True`` checks it with ``validate_definition``
    (errors raise ``WorkflowNotReady``) and publishes it, replacing the same automation if it exists.
    """
    from apps.automation.models import Workflow
    from apps.automation.workflow_engine import validate_definition

    existing = Workflow.objects.filter(account=account, slug=slug).first()
    if not turn_on:
        if existing is not None and existing.status == Workflow.Status.PUBLISHED:
            n = 2
            while Workflow.objects.filter(
                account=account, slug=f"{slug}-draft-{n}"[:50]
            ).exists():
                n += 1
            slug, existing = f"{slug}-draft-{n}"[:50], None
        if existing is None:
            return Workflow.objects.create(
                account=account,
                slug=slug,
                name=name,
                definition=definition,
                status=Workflow.Status.DRAFT,
            )
        existing.name, existing.definition, existing.status = (
            name,
            definition,
            Workflow.Status.DRAFT,
        )
        existing.save(update_fields=["name", "definition", "status", "updated_at"])
        return existing

    blocking = [
        e
        for e in validate_definition(definition, account=account)
        if e.get("severity", "error") != "warning"
    ]
    if blocking:
        raise WorkflowNotReady(blocking[0]["message"])
    workflow = existing or Workflow(account=account, slug=slug)
    try:
        ensure_room_to_turn_on(account, workflow)
    except AutomationLimitReached as exc:
        raise WorkflowNotReady(str(exc)) from exc
    if workflow.pk and workflow.status != Workflow.Status.PUBLISHED:
        workflow.version += 1
    from django.db import transaction

    from apps.contacts.attributes import verify_defs_for_publish

    workflow.name, workflow.definition, workflow.status = (
        name,
        definition,
        Workflow.Status.PUBLISHED,
    )
    with transaction.atomic():
        verify_defs_for_publish(account, definition)
        workflow.save()
    return workflow


def automate_offers(account, message_ids) -> dict:
    """``{message_id: {"count", "url"}}`` for replies the team keeps sending that aren't automated
    yet: the inbox offers "You've answered this N times. Turn it into an automation?"."""
    from django.urls import reverse

    from apps.automation import patterns

    base = reverse("build:review")
    return {
        mid: {"count": o["count"], "url": f"{base}?pattern={o['key']}"}
        for mid, o in patterns.offers_for_messages(account, message_ids).items()
    }


def repeated_question_offer(account, text: str) -> dict | None:
    """``{"count", "url"}`` when ``text`` matches a question the team keeps answering by hand."""
    from django.urls import reverse

    from apps.automation import keywords, patterns

    for pattern in patterns.open_patterns(account):
        if keywords.matches(
            {"mode": "contains", "any": pattern.question_keywords}, text or ""
        ):
            return {
                "count": pattern.count,
                "url": f"{reverse('build:review')}?pattern={pattern.key}",
            }
    return None


def published_trigger_matches(account) -> list[dict]:
    """``[{"slug", "name", "match"}]`` for live message automations with keyword triggers."""
    from apps.automation.models import Workflow

    out = []
    for wf in Workflow.objects.filter(
        account=account, status=Workflow.Status.PUBLISHED
    ):
        trigger = (wf.definition or {}).get("trigger") or {}
        if trigger.get("match"):
            out.append({"slug": wf.slug, "name": wf.name, "match": trigger["match"]})
    return out


def adoption(account, *, since=None) -> dict:
    """``{"on", "first_run_at"}``: automations turned on now, and when one first ran for a customer
    (the pilot's "time to first value"; no publish date is stored, and a run is the stronger signal).

    ``since`` (e.g. when WhatsApp connected) ignores earlier runs, such as email automations that
    ran before the business came on WhatsApp.
    """
    from django.db.models import Min

    from apps.automation.models import Workflow, WorkflowRun

    runs = WorkflowRun.objects.filter(workflow__account=account)
    if since is not None:
        runs = runs.filter(started_at__gte=since)
    return {
        "on": Workflow.objects.filter(
            account=account, status=Workflow.Status.PUBLISHED
        ).count(),
        "first_run_at": runs.aggregate(first=Min("started_at"))["first"],
    }


def recent_failed_runs(limit: int = 5) -> list[dict]:
    """The latest failed automation runs across all businesses, for staff."""
    from apps.automation.models import WorkflowRun

    runs = (
        WorkflowRun.objects.filter(status=WorkflowRun.Status.FAILED)
        .select_related("workflow__account")
        .prefetch_related("step_runs")
        .order_by("-completed_at", "-started_at")[:limit]
    )
    out = []
    for run in runs:
        failed = [s for s in run.step_runs.all() if s.status != "ok"]
        step = failed[-1] if failed else None
        out.append(
            {
                "account": run.workflow.account,
                "workflow": run.workflow.name,
                "at": run.completed_at or run.started_at,
                "step": step.step_type if step else "",
                "error": str((step.result or {}).get("error", "")) if step else "",
            }
        )
    return out


def out_of_hours_reply_on(account) -> bool:
    """Whether a live automation answers differently when the business is closed (it branches on
    ``within_business_hours``, as the "Reply when you're closed" starter does)."""
    import json

    from apps.automation.models import Workflow

    return any(
        "within_business_hours" in json.dumps(definition or {})
        for definition in Workflow.objects.filter(
            account=account, status=Workflow.Status.PUBLISHED
        ).values_list("definition", flat=True)
    )


def published_slugs(account) -> set:
    from apps.automation.models import Workflow

    return set(
        Workflow.objects.filter(
            account=account, status=Workflow.Status.PUBLISHED
        ).values_list("slug", flat=True)
    )


# Re-export for Phase 2 compatibility (triggers still imports from here internally)
# This will be removed once triggers.py is fully migrated to use dispatcher
from apps.automation.triggers import (  # noqa: E402, F401
    on_message_received,
    on_message_sent,
)


def activity_for_contact(account: Account, contact, *, limit: int = 8) -> list[dict]:
    """What automations did for this customer lately, newest first, in owner words.

    Each item is ``{"when", "automation", "lines": [{"ok", "text"}], "problem"}``. Tenant-scoped by
    ``account`` as well as ``contact``.
    """
    from apps.automation import explain
    from apps.automation.models import WorkflowRun

    runs = (
        WorkflowRun.objects.filter(workflow__account=account, contact=contact)
        .select_related("workflow", "version")
        .prefetch_related("step_runs")
        .order_by("-started_at")[:limit]
    )
    items = []
    for run in runs:
        from apps.automation.workflow_engine import run_definition

        steps = {s.get("id"): s for s in run_definition(run).get("steps", [])}
        lines = [
            explain.describe_step(
                sr.step_type, sr.status, sr.result, steps.get(sr.step_id)
            )
            for sr in sorted(run.step_runs.all(), key=lambda sr: sr.executed_at)
            if sr.step_type not in ("branch", "stop")
        ]
        items.append(
            {
                "when": run.started_at,
                "automation": run.workflow.name,
                "lines": lines,
                "problem": any(not line["ok"] for line in lines),
            }
        )
    return items


# ── Lifecycle: one implementation behind the editor, the list and the public API ──────────


def editable_definition(workflow) -> dict:
    """What the editor shows: unpublished changes if there are any, else what runs."""
    if workflow.draft_definition is not None:
        return workflow.draft_definition
    return workflow.definition or {}


def save_edits(workflow, definition: dict) -> None:
    """Save the editor's work. A draft is edited in place; a workflow that's on (or paused)
    keeps running its current steps and holds the edits until "Publish changes"."""
    from apps.automation.models import Workflow

    if workflow.status == Workflow.Status.DRAFT:
        workflow.definition = definition
        workflow.draft_definition = None
    else:
        workflow.draft_definition = (
            None if definition == (workflow.definition or {}) else definition
        )
    workflow.save(update_fields=["definition", "draft_definition", "updated_at"])


def discard_edits(workflow) -> None:
    workflow.draft_definition = None
    workflow.save(update_fields=["draft_definition", "updated_at"])


def publish(workflow, definition: dict | None = None):
    """Turn ``workflow`` on with ``definition`` (default: its pending edits, else its
    current steps). Raises ``WorkflowNotReady`` with the first problem, or
    ``AutomationLimitReached``. Runs already in flight keep the version they started on."""
    from django.db import transaction

    from apps.automation.models import Workflow
    from apps.contacts.attributes import verify_defs_for_publish

    if definition is None:
        definition = editable_definition(workflow)
    problems = live_definition_problems(workflow.account, definition)
    if problems:
        raise WorkflowNotReady(problems[0]["message"])
    ensure_room_to_turn_on(workflow.account, workflow)
    resuming = workflow.status == Workflow.Status.PAUSED
    with transaction.atomic():
        try:
            verify_defs_for_publish(workflow.account, definition)
        except ValueError as exc:
            raise WorkflowNotReady(str(exc)) from exc
        changed = definition != (workflow.definition or {})
        if changed or workflow.status != Workflow.Status.PUBLISHED:
            workflow.version += 1
        workflow.definition = definition
        workflow.draft_definition = None
        if resuming:
            _shift_waiting_runs(workflow)
        workflow.status = Workflow.Status.PUBLISHED
        workflow.paused_at = None
        workflow.save(
            update_fields=[
                "definition",
                "draft_definition",
                "version",
                "status",
                "paused_at",
                "updated_at",
            ]
        )
    return workflow


def pause(workflow) -> None:
    """Stop starting new runs and hold waiting customers where they are."""
    from django.utils import timezone

    from apps.automation.models import Workflow

    if workflow.status != Workflow.Status.PUBLISHED:
        return
    workflow.status = Workflow.Status.PAUSED
    workflow.paused_at = timezone.now()
    workflow.save(update_fields=["status", "paused_at", "updated_at"])


def resume(workflow, *, waiting: str = "continue") -> None:
    """Turn a paused or archived workflow back on, as it was (no new version).

    ``waiting="continue"`` gives held customers back the time they lost while paused, so a
    "check in after 3 days" still waits 3 days of running time. ``"cancel"`` lets them go.
    Raises ``WorkflowNotReady`` / ``AutomationLimitReached`` like ``publish``.
    """
    from django.db import transaction

    from apps.automation.models import Workflow
    from apps.contacts.attributes import verify_defs_for_publish

    if workflow.status not in (Workflow.Status.PAUSED, Workflow.Status.ARCHIVED):
        return
    problems = live_definition_problems(workflow.account, workflow.definition)
    if problems:
        raise WorkflowNotReady(problems[0]["message"])
    ensure_room_to_turn_on(workflow.account, workflow)
    with transaction.atomic():
        try:
            verify_defs_for_publish(workflow.account, workflow.definition)
        except ValueError as exc:
            raise WorkflowNotReady(str(exc)) from exc
        if workflow.status == Workflow.Status.PAUSED:
            if waiting == "cancel":
                end_runs(workflow)
            else:
                _shift_waiting_runs(workflow)
        workflow.status = Workflow.Status.PUBLISHED
        workflow.paused_at = None
        workflow.save(update_fields=["status", "paused_at", "updated_at"])


def archive(workflow) -> int:
    """Turn ``workflow`` off for good: nothing new starts and everyone in it is let go.
    Returns how many runs were ended."""
    from apps.automation.models import Workflow

    workflow.status = Workflow.Status.ARCHIVED
    workflow.paused_at = None
    workflow.save(update_fields=["status", "paused_at", "updated_at"])
    return end_runs(workflow)


def end_runs(workflow) -> int:
    """Cancel every active or waiting run of ``workflow`` (and their open questions)."""
    from django.utils import timezone

    from apps.automation.interaction import cancel_open_interactions_for_run
    from apps.automation.models import WorkflowRun

    runs = list(
        WorkflowRun.objects.filter(
            workflow=workflow,
            status__in=[WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING],
        )
    )
    now = timezone.now()
    for run in runs:
        run.status = WorkflowRun.Status.CANCELLED
        run.completed_at = now
        run.save(update_fields=["status", "completed_at"])
        cancel_open_interactions_for_run(run)
    return len(runs)


def _shift_waiting_runs(workflow) -> None:
    """Push waiting runs' timers back by how long ``workflow`` was paused."""
    from django.db.models import F
    from django.utils import timezone

    from apps.automation.models import WorkflowInteraction, WorkflowRun

    if workflow.paused_at is None:
        return
    gap = timezone.now() - workflow.paused_at
    WorkflowRun.objects.filter(
        workflow=workflow,
        status=WorkflowRun.Status.WAITING,
        next_due_at__isnull=False,
    ).update(next_due_at=F("next_due_at") + gap)
    # A reply question's own deadline re-arms the run on retries, so it moves too.
    WorkflowInteraction.objects.filter(
        run__workflow=workflow,
        status=WorkflowInteraction.Status.OPEN,
        deadline_at__isnull=False,
    ).update(deadline_at=F("deadline_at") + gap)


# ── Run history: fix one customer's run from the timeline ────────────────────────────────


class RunActionError(ValueError):
    """The run can't be retried, skipped or cancelled right now. The message is for the owner."""


def _failed_step(run):
    return run.step_runs.filter(status__in=["error", "failed"]).order_by("-id").first()


def _ensure_no_newer_run(run) -> None:
    from apps.automation.models import WorkflowRun

    if (
        WorkflowRun.objects.filter(
            workflow=run.workflow,
            contact=run.contact,
            subject_key=run.subject_key,
            status__in=[WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING],
        )
        .exclude(pk=run.pk)
        .exists()
    ):
        raise RunActionError(
            "This customer is already part-way through this automation again, so this "
            "older run can't be restarted."
        )


def retry_run(run):
    """Try a failed run's failing step again (a fresh send), then carry on from there."""
    from django.db import transaction

    from apps.automation.models import WorkflowRun
    from apps.automation.workflow_engine import advance_run

    if run.status != WorkflowRun.Status.FAILED:
        raise RunActionError("Only a run that failed can be retried.")
    failed = _failed_step(run)
    if failed is None:
        raise RunActionError("There's no failed step to retry.")
    _ensure_no_newer_run(run)
    with transaction.atomic():
        retries = dict((run.context or {}).get("_retries") or {})
        retries[failed.step_id] = retries.get(failed.step_id, 0) + 1
        run.context = {**(run.context or {}), "_retries": retries}
        failed.delete()
        run.status = WorkflowRun.Status.ACTIVE
        run.current_step = failed.step_id
        run.completed_at = None
        run.next_due_at = None
        run.save(
            update_fields=[
                "context",
                "status",
                "current_step",
                "completed_at",
                "next_due_at",
            ]
        )
        advance_run(run)
    run.refresh_from_db()
    return run


def skip_failed_step(run):
    """Mark a failed run's failing step as skipped and carry on with the step after it."""
    from django.db import transaction

    from apps.automation.models import WorkflowRun
    from apps.automation.workflow_engine import _run_steps, advance_run

    if run.status != WorkflowRun.Status.FAILED:
        raise RunActionError("Only a run that failed can skip a step.")
    failed = _failed_step(run)
    if failed is None:
        raise RunActionError("There's no failed step to skip.")
    _ensure_no_newer_run(run)
    step = _run_steps(run).get(failed.step_id) or {}
    with transaction.atomic():
        failed.status = "skipped"
        failed.save(update_fields=["status"])
        run.status = WorkflowRun.Status.ACTIVE
        run.current_step = step.get("next", "")
        run.completed_at = None
        run.next_due_at = None
        run.save(
            update_fields=["status", "current_step", "completed_at", "next_due_at"]
        )
        advance_run(run)
    run.refresh_from_db()
    return run


def cancel_run(run):
    """Stop a run that's still going (and any question it's waiting on)."""
    from django.utils import timezone

    from apps.automation.interaction import cancel_open_interactions_for_run
    from apps.automation.models import WorkflowRun

    if run.status not in (WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING):
        raise RunActionError("This run has already finished.")
    run.status = WorkflowRun.Status.CANCELLED
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "completed_at"])
    cancel_open_interactions_for_run(run)
    return run


def run_timeline(run) -> list[dict]:
    """What happened in ``run``, step by step, in owner words."""
    from apps.automation import explain
    from apps.automation.labels import step_label
    from apps.automation.workflow_engine import _run_steps

    steps = _run_steps(run)
    items = []
    for sr in run.step_runs.order_by("executed_at", "id"):
        described = explain.describe_step(
            sr.step_type, sr.status, sr.result, steps.get(sr.step_id)
        )
        step = steps.get(sr.step_id) or {}
        items.append(
            {
                "step_id": sr.step_id,
                "step_run_id": sr.pk,
                "ai_labels": [
                    i.get("label")
                    for i in step.get("labels") or []
                    if isinstance(i, dict)
                ]
                if sr.step_type == "ai_classify"
                else [],
                "ai_label": (sr.result or {}).get("corrected")
                or (sr.result or {}).get("label")
                or "",
                "label": step_label(sr.step_type),
                "at": sr.executed_at,
                "ok": described["ok"],
                "text": described["text"],
                "status": sr.status,
                "error": (sr.result or {}).get("error", "")
                if sr.status in ("error", "failed")
                else "",
            }
        )
    return items


# ── Manual runs: "Run automation" from a customer or conversation ─────────────────────────


def manual_workflows(account):
    """Automations that are on and start by hand (``manual`` trigger), by name."""
    from apps.automation.models import Workflow

    return [
        wf
        for wf in Workflow.objects.filter(
            account=account, status=Workflow.Status.PUBLISHED
        ).order_by("name")
        if ((wf.definition or {}).get("trigger") or {}).get("type") == "manual"
    ]


def enroll_manually(workflow, contacts, *, context: dict | None = None) -> dict:
    """Start ``workflow`` for each contact. ``{"started": n, "already": n, "skipped": n}``.

    "already": they're part-way through it; "skipped": it couldn't start for them (opted
    out of WhatsApp, the plan doesn't include automations, ...).
    """
    from django.db import transaction

    from apps.automation.models import WorkflowRun
    from apps.automation.workflow_engine import enroll

    counts = {"started": 0, "already": 0, "skipped": 0}
    for contact in contacts:
        existing = WorkflowRun.objects.filter(
            workflow=workflow,
            contact=contact,
            subject_key="",
            status__in=[WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING],
        ).exists()
        if existing:
            counts["already"] += 1
            continue
        with transaction.atomic():
            run = enroll(workflow, contact, context=dict(context or {}))
        counts["started" if run is not None else "skipped"] += 1
    return counts


def contact_automations(account, contact) -> dict:
    """For a customer's page: what's running for them now, and what can be started by hand."""
    from apps.automation.labels import step_label
    from apps.automation.models import WorkflowRun
    from apps.automation.workflow_engine import _run_steps, waiting_on_customer

    running = []
    for run in (
        WorkflowRun.objects.filter(
            workflow__account=account,
            contact=contact,
            status__in=[WorkflowRun.Status.ACTIVE, WorkflowRun.Status.WAITING],
        )
        .select_related("workflow", "version")
        .order_by("-started_at")[:10]
    ):
        step = _run_steps(run).get(run.current_step) or {}
        if waiting_on_customer(run):
            state = "Waiting for their answer"
        elif run.status == WorkflowRun.Status.WAITING:
            state = "Waiting"
        else:
            state = "Running"
        running.append(
            {
                "run_id": run.public_id,
                "workflow": run.workflow.name,
                "step": step_label(step.get("type", "")) if step else "",
                "state": state,
                "next_due_at": run.next_due_at,
                "conversation_id": (run.context or {}).get("conversation_id", ""),
            }
        )
    return {"running": running, "manual": manual_workflows(account)}


# ── Forms: carry on from a ``start_form`` step ────────────────────────────────────────────


def form_finished(response, *, outcome: str) -> int:
    """A form a run was waiting on was completed (``outcome="completed"``) or let go
    (``"timed_out"``): continue each such run down the matching path. Returns how many."""
    from django.db import transaction

    from apps.automation.models import WorkflowRun
    from apps.automation.workflow_engine import _run_steps, advance_run

    moved = 0
    candidates = WorkflowRun.objects.filter(
        workflow__account_id=response.account_id,
        contact_id=response.contact_id,
        status=WorkflowRun.Status.WAITING,
    )
    for candidate in candidates:
        with transaction.atomic():
            run = WorkflowRun.objects.select_for_update().get(pk=candidate.pk)
            if run.status != WorkflowRun.Status.WAITING:
                continue
            step = _run_steps(run).get(run.current_step) or {}
            waiting_for = ((run.context or {}).get("_form_responses") or {}).get(
                run.current_step
            )
            if step.get("type") != "start_form" or waiting_for != response.pk:
                continue
            run.current_step = (
                step.get("next", "")
                if outcome == "completed"
                else step.get("on_timeout", "")
            )
            run.status = WorkflowRun.Status.ACTIVE
            run.next_due_at = None
            if outcome == "completed":
                run.context = {
                    **(run.context or {}),
                    "form_answers": {
                        k: (", ".join(v) if isinstance(v, list) else v)
                        for k, v in (response.answers or {}).items()
                    },
                }
            run.save(update_fields=["current_step", "status", "next_due_at", "context"])
            if run.current_step:
                advance_run(run)
            else:
                from apps.automation.workflow_engine import _complete

                _complete(run)
            moved += 1
    return moved


# ── AI steps: corrections and how well they're doing ─────────────────────────────────────


def correct_ai_label(step_run, label: str) -> None:
    """Record what an ``ai_classify`` decision should have been. The run isn't re-routed (it
    already happened); the correction becomes an example for that step's next decisions."""
    from apps.automation.workflow_engine import _run_steps

    step = _run_steps(step_run.run).get(step_run.step_id) or {}
    labels = [i.get("label") for i in step.get("labels") or [] if isinstance(i, dict)]
    if step_run.step_type != "ai_classify" or label not in labels:
        raise RunActionError("Choose one of this step's labels.")
    step_run.result = {**(step_run.result or {}), "corrected": label}
    step_run.save(update_fields=["result"])


def ai_quality(workflow) -> list[dict]:
    """Per AI step: how often it decided, wasn't sure or was corrected, in plain words."""
    from apps.automation.labels import step_label
    from apps.automation.models import WorkflowStepRun

    out = []
    steps = [
        s
        for s in (workflow.definition or {}).get("steps", [])
        if s.get("type") in ("ai_classify", "ai_extract", "ai_answer")
    ]
    for step in steps:
        rows = list(
            WorkflowStepRun.objects.filter(run__workflow=workflow, step_id=step["id"])
            .order_by("-executed_at")
            .values_list("result", flat=True)[:500]
        )
        total = len(rows)
        if step["type"] == "ai_classify":
            unsure = sum(1 for r in rows if not r.get("label"))
            corrected = sum(
                1
                for r in rows
                if r.get("corrected") and r.get("corrected") != r.get("label")
            )
            fell_back = unsure
        elif step["type"] == "ai_extract":
            fell_back = sum(1 for r in rows if r.get("missing"))
            corrected = 0
        else:
            fell_back = sum(1 for r in rows if r.get("handed_off"))
            corrected = 0
        rate = (fell_back / total) if total else 0
        if not total:
            verdict = "Not used yet."
        elif step["type"] == "ai_classify" and corrected and corrected / total > 0.1:
            verdict = "Often corrected: add example messages to the labels."
        elif rate > 0.4:
            verdict = (
                "Often unsure: add a label or example messages."
                if step["type"] == "ai_classify"
                else "Often falls back: check what customers usually send here."
            )
        else:
            verdict = "Usually decides on its own."
        out.append(
            {
                "step_id": step["id"],
                "label": step_label(step["type"]),
                "total": total,
                "fell_back": fell_back,
                "corrected": corrected,
                "verdict": verdict,
            }
        )
    return out


# ── Before publishing: would it catch what customers actually say? ──────────────────────


_CHECK_TEXT_FIELDS = ("text", "question", "note")


def fact_warnings(account, definition: dict) -> list[dict]:
    """Replies that state prices, times or places your notes, hours and answers don't back up.
    Deterministic (``apps.ai.facts``); shown as warnings, never blocking."""
    from apps.ai import api as ai_api
    from apps.automation import variables

    out = []
    for step in (definition or {}).get("steps") or []:
        for field in _CHECK_TEXT_FIELDS:
            text = step.get(field)
            if not isinstance(text, str) or not text.strip():
                continue
            shown = variables.merge_first_name(
                variables.merge_business_facts(text, account), "Sam"
            )
            unchecked = ai_api.unchecked_facts(account, shown)
            if unchecked:
                out.append(
                    {
                        "step_id": step.get("id"),
                        "field": field,
                        "severity": "warning",
                        "message": "Check "
                        + ", ".join(unchecked[:3])
                        + ": it isn't in your notes, hours or answers, so make sure it's right.",
                    }
                )
    return out


def coverage(account, workflow, definition: dict, *, extra_phrases=()) -> dict:
    """Which realistic messages the trigger catches, which it misses, and which other
    automations would also want them. The real keyword matcher decides; AI (if on) only
    writes test phrases and suggests words."""
    from apps.ai import api as ai_api
    from apps.automation import keywords
    from apps.automation.models import Workflow
    from apps.conversations.models import Message

    trigger = (definition or {}).get("trigger") or {}
    match = trigger.get("match")
    words = list((match or {}).get("any") or [])
    if trigger.get("type") not in (
        "conversation.message_received",
        "whatsapp.received",
    ):
        return {"applies": False}
    real = list(
        Message.objects.filter(account=account, direction=Message.Direction.INBOUND)
        .exclude(body="")
        .order_by("-timestamp")
        .values_list("body", flat=True)[:300]
    )
    # Real recent messages it would have caught: unrelated ones would only read as noise.
    real = [r for r in real if not match or keywords.matches(match, r)][:6]
    about = workflow.name + (": " + ", ".join(words) if words else "")
    generated = ai_api.sample_phrases(
        account,
        about=about,
        keywords=words,
        operation_id=f"wf-coverage:{workflow.pk}:{len(words)}",
    )
    phrases = list(dict.fromkeys([*extra_phrases, *generated["phrases"], *real]))[:24]
    others = [
        w
        for w in Workflow.objects.filter(
            account=account, status=Workflow.Status.PUBLISHED
        ).exclude(pk=workflow.pk)
        if ((w.definition or {}).get("trigger") or {}).get("type")
        in ("conversation.message_received", "whatsapp.received")
    ]
    other_matches = [
        (w.name, ((w.definition or {}).get("trigger") or {}).get("match"))
        for w in others
    ]
    rows = []
    for phrase in phrases:
        caught = (not match) or keywords.matches(match, phrase)
        overlap = [
            name for name, m in other_matches if (not m) or keywords.matches(m, phrase)
        ]
        rows.append({"text": phrase, "caught": caught, "also": overlap[:2]})
    return {
        "applies": True,
        "ai": bool(generated["phrases"]),
        "rows": rows,
        "caught": sum(1 for r in rows if r["caught"]),
        "total": len(rows),
        "suggested_keywords": generated["keywords"],
    }

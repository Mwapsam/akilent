"""Dashboard: lifecycle workflow list + visual/code builder (Phase 6 UI)."""
from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun
from apps.automation.workflow_engine import validate_definition
from apps.automation.workflow_templates import STARTER_TEMPLATES, list_templates

_STEP_TYPES = ["send_email", "send_whatsapp", "webhook", "wait", "branch", "set_attribute", "stop"]
_TRIGGER_TYPES = ["manual", "business_event", "contact.created", "contact.updated",
                  "email.opened", "email.clicked"]


@login_required
def workflow_list(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    workflows = Workflow.objects.filter(account=account)
    return render(request, "automation/workflow_list.html", {
        "account": account,
        "workflows": workflows,
        "starters": list_templates(),
    })


@login_required
@require_POST
def workflow_create(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    starter = request.POST.get("from_template") or ""
    name = (request.POST.get("name") or "").strip()
    definition: dict = {"trigger": {"type": "manual"}, "steps": [{"id": "stop", "type": "stop"}]}
    if starter and starter in STARTER_TEMPLATES:
        tpl = STARTER_TEMPLATES[starter]
        definition = tpl["definition"]
        name = name or tpl["name"]
    if not name:
        messages.error(request, "Give the workflow a name.")
        return redirect("automation:list")

    slug = slugify(name)[:160] or "workflow"
    base, i = slug, 2
    while Workflow.objects.filter(account=account, slug=slug).exists():
        slug = f"{base}-{i}"
        i += 1
    wf = Workflow.objects.create(account=account, name=name, slug=slug, definition=definition)
    return redirect("automation:editor", slug=wf.slug)


@login_required
def workflow_editor(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)

    from apps.email.models import EmailTemplate
    from apps.whatsapp.models import MessageTemplate

    email_templates = [
        {"slug": t.slug, "name": t.name, "subject": t.subject}
        for t in EmailTemplate.objects.filter(account=account, is_active=True).order_by("name")
    ]
    whatsapp_templates = [
        {
            "name": t.whatsapp_template_name,
            "label": t.name,
            "language_code": t.language_code,
            "approved": t.approval_status == t.ApprovalStatus.APPROVED,
            "variables": t.variables,
        }
        for t in MessageTemplate.objects.filter(account=account)
        .exclude(whatsapp_template_name__isnull=True)
        .exclude(whatsapp_template_name="")
        .order_by("name")
    ]

    return render(request, "automation/workflow_editor.html", {
        "account": account,
        "wf": wf,
        "definition_json": json.dumps(wf.definition or {}, indent=2),
        "step_types": _STEP_TYPES,
        "trigger_types": _TRIGGER_TYPES,
        "validation_errors": json.dumps(validate_definition(wf.definition, account=account)),
        "email_templates_json": json.dumps(email_templates),
        "whatsapp_templates_json": json.dumps(whatsapp_templates),
    })


@login_required
def workflow_stats(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)

    run_counts = {
        row["status"]: row["count"]
        for row in WorkflowRun.objects.filter(workflow=wf).values("status").annotate(count=Count("id"))
    }
    total_enrolled = sum(run_counts.values())

    step_rows = list(
        WorkflowStepRun.objects.filter(run__workflow=wf)
        .values("step_id", "step_type", "status")
        .annotate(count=Count("id"))
        .order_by("step_id", "status")
    )
    steps_by_id: dict[str, dict] = {}
    for row in step_rows:
        entry = steps_by_id.setdefault(row["step_id"], {
            "step_id": row["step_id"], "step_type": row["step_type"], "ok": 0, "error": 0,
        })
        if row["status"] == "error":
            entry["error"] += row["count"]
        else:
            entry["ok"] += row["count"]

    return render(request, "automation/workflow_stats.html", {
        "account": account,
        "wf": wf,
        "total_enrolled": total_enrolled,
        "run_counts": run_counts,
        "step_breakdown": list(steps_by_id.values()),
    })


@login_required
@require_POST
def workflow_save(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)

    if isinstance(body.get("name"), str) and body["name"].strip():
        wf.name = body["name"].strip()
    if isinstance(body.get("definition"), dict):
        wf.definition = body["definition"]
    wf.save(update_fields=["name", "definition", "updated_at"])
    return JsonResponse({
        "ok": True,
        "errors": validate_definition(wf.definition, account=account),
        "status": wf.status,
        "version": wf.version,
    })


@login_required
@require_POST
def workflow_publish(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    errors = validate_definition(wf.definition, account=account)
    blocking = [e for e in errors if e.get("severity", "error") != "warning"]
    if blocking:
        messages.error(
            request,
            "Fix the workflow before publishing: " + "; ".join(e["message"] for e in blocking[:3]),
        )
        return redirect("automation:editor", slug=wf.slug)
    if wf.status != Workflow.Status.PUBLISHED:
        wf.version += 1
    wf.status = Workflow.Status.PUBLISHED
    wf.save(update_fields=["status", "version", "updated_at"])
    messages.success(request, f"Published {wf.name} (v{wf.version}).")
    return redirect("automation:editor", slug=wf.slug)


@login_required
@require_POST
def workflow_archive(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    wf.status = Workflow.Status.ARCHIVED
    wf.save(update_fields=["status", "updated_at"])
    messages.success(request, f"Archived {wf.name}.")
    return redirect("automation:list")


@login_required
@require_POST
def workflow_delete(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    wf.delete()
    messages.success(request, "Workflow deleted.")
    return redirect("automation:list")

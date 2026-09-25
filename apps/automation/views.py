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
from apps.automation import api as automation_api
from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun
from apps.automation.workflow_engine import validate_definition
from apps.automation.workflow_templates import STARTER_TEMPLATES, list_templates
from apps.core.module_gate import module_required

_STEP_TYPES = ["send_email", "send_whatsapp", "reply_text", "send_buttons", "send_list", "wait_for_reply",
               "add_tag", "remove_tag", "webhook", "wait", "branch", "set_attribute", "stop"]
_TRIGGER_TYPES = ["manual", "business_event", "contact.created", "contact.updated",
                  "email.opened", "email.clicked", "conversation.message_received"]


@login_required
@module_required("automation")
def workflow_list(request):
    from apps.automation.labels import trigger_label

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    workflows = list(Workflow.objects.filter(account=account))
    for wf in workflows:
        # Computed here, not in the template: a dict without a "name" key makes
        # `{{ trigger.name }}` used as a *filter argument* raise
        # VariableDoesNotExist uncaught (unlike a plain variable, filter
        # arguments don't get the template engine's safe string_if_invalid
        # fallback) — crashed this page in production. See friendly_errors.py
        # note in apps.whatsapp for the same "translate once, in Python" rule.
        trigger = (wf.definition or {}).get("trigger") or {}
        wf.trigger_label = trigger_label(trigger.get("type", ""), trigger.get("name", ""))
    engagement, approved_templates = _engagement_starters(account, workflows)
    return render(request, "automation/workflow_list.html", {
        "account": account,
        "workflows": workflows,
        "starters": list_templates(),
        "engagement_starters": engagement,
        "approved_templates": approved_templates,
    })


def _engagement_starters(account, workflows):
    """The one-click follow-up cards, each told whether it's already on.

    A card that can't work is said so plainly rather than installed and left to
    fail at send time: WhatsApp only delivers templates Meta has approved, so
    with no approved message there is nothing to send.
    """
    from apps.automation.engagement_starters import ENGAGEMENT_STARTERS
    from apps.whatsapp.models import MessageTemplate

    approved = list(
        MessageTemplate.objects.filter(
            account=account, approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        ).order_by("name")
    )
    installed = {wf.slug: wf for wf in workflows if wf.status == Workflow.Status.PUBLISHED}
    cards = [
        {**starter, "installed": installed.get(starter["key"])}
        for starter in ENGAGEMENT_STARTERS
    ]
    return cards, approved


@login_required
@module_required("automation")
@require_POST
def starter_install(request):
    """Install an engagement follow-up in one click — published and running.

    The owner picks which of their approved templates to send, because only
    Meta-approved templates can be sent and only they know which of theirs fits.
    Installing the same starter again updates it in place (the slug is the
    starter key), so a second click changes the template rather than leaving two
    workflows chasing the same customers.
    """
    from apps.automation.engagement_starters import STARTERS_BY_KEY, build_definition
    from apps.whatsapp.models import MessageTemplate

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    starter = STARTERS_BY_KEY.get(request.POST.get("starter") or "")
    if starter is None:
        messages.error(request, "That follow-up isn't available.")
        return redirect("automation:list")

    if starter.get("menu"):
        return _install_menu_starter(request, account, starter)
    if starter.get("reply"):
        return _install_reply_starter(request, account, starter)

    template = MessageTemplate.objects.filter(
        account=account, pk=request.POST.get("template_id") or 0,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    ).first()
    if template is None:
        messages.error(request, "Choose an approved message to send.")
        return redirect("automation:list")

    # Namespaced per template, like the conversation composer's template
    # picker: an x-show'd field is still submitted, so an unqualified name
    # could silently read a different template's stale value.
    variable_mapping = {
        var: value
        for var in (template.variables or [])
        if (value := (request.POST.get(f"var__{template.pk}__{var}") or "").strip())
    }
    missing = [var for var in (template.variables or []) if var not in variable_mapping]
    if missing:
        messages.error(request, "Fill in what should go in every blank of the message.")
        return redirect("automation:list")

    automation_api.upsert_published_workflow(
        account,
        slug=starter["key"],
        name=starter["name"],
        definition=build_definition(
            starter, template_name=template.whatsapp_template_name,
            variable_mapping=variable_mapping,
        ),
    )
    messages.success(request, f"{starter['name']} is on. {starter['stop_condition']}")
    return redirect("automation:list")


_MAX_REPLY_LENGTH = 1000
_MENU_OPTIONS = 3


def _install_menu_starter(request, account, starter):
    """Install a guided menu from the owner's question and up to three options with answers."""
    from apps.automation.engagement_starters import build_menu_definition
    from apps.whatsapp import interactive as wa_interactive

    question = (request.POST.get("menu_text") or "").strip()
    options = []
    for n in range(1, _MENU_OPTIONS + 1):
        title = (request.POST.get(f"opt_title_{n}") or "").strip()
        reply = (request.POST.get(f"opt_reply_{n}") or "").strip()
        if not title and not reply:
            continue
        if not title or not reply:
            messages.error(request, f"Option {n} needs both a button label and an answer.")
            return redirect("automation:list")
        if len(reply) > _MAX_REPLY_LENGTH:
            messages.error(request, f"Keep option {n}'s answer under {_MAX_REPLY_LENGTH} characters.")
            return redirect("automation:list")
        options.append({"title": title, "reply": reply})
    if not options:
        messages.error(request, "Add at least one button and its answer.")
        return redirect("automation:list")
    try:
        # The same checks WhatsApp's limits imply, so a bad label is caught here in plain words.
        wa_interactive.build_buttons(question, [{"title": o["title"]} for o in options])
    except wa_interactive.InteractiveError as exc:
        messages.error(request, str(exc))
        return redirect("automation:list")
    automation_api.upsert_published_workflow(
        account, slug=starter["key"], name=starter["name"],
        definition=build_menu_definition(starter, question=question, options=options),
    )
    messages.success(request, f"{starter['name']} is on. {starter['stop_condition']}")
    return redirect("automation:list")


def _install_reply_starter(request, account, starter):
    """Install a keyword auto-reply: the owner's own words, sent as a normal message."""
    from apps.automation.engagement_starters import build_reply_definition

    if starter.get("needs_hours"):
        from apps.accounts import business_hours

        if not business_hours.is_configured(account):
            messages.error(
                request,
                "Set your opening hours first (Settings, then Opening hours), so Akilent knows "
                "when you're closed.",
            )
            return redirect("automation:list")
    text = (request.POST.get("reply_text") or "").strip()
    if not text:
        messages.error(request, "Write the reply customers should get.")
        return redirect("automation:list")
    if len(text) > _MAX_REPLY_LENGTH:
        messages.error(request, f"Keep the reply under {_MAX_REPLY_LENGTH} characters.")
        return redirect("automation:list")
    automation_api.upsert_published_workflow(
        account, slug=starter["key"], name=starter["name"],
        definition=build_reply_definition(starter, text=text),
    )
    messages.success(request, f"{starter['name']} is on. {starter['stop_condition']}")
    return redirect("automation:list")


@login_required
@module_required("automation")
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
@module_required("automation")
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
        "step_types": json.dumps(_STEP_TYPES),
        "trigger_types": json.dumps(_TRIGGER_TYPES),
        "validation_errors": json.dumps(validate_definition(wf.definition, account=account)),
        "email_templates_json": json.dumps(email_templates),
        "whatsapp_templates_json": json.dumps(whatsapp_templates),
    })


@login_required
@module_required("automation")
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

    # The page's own question is "where are people dropping out", which is a
    # rate, not a count: 50 failures out of 10,000 is noise, 50 out of 60 is
    # the answer. Computed here so the template compares like with like.
    breakdown = list(steps_by_id.values())
    for entry in breakdown:
        entry["total"] = entry["ok"] + entry["error"]
        entry["error_rate"] = (
            round(entry["error"] / entry["total"] * 100, 1) if entry["total"] else 0
        )

    return render(request, "automation/workflow_stats.html", {
        "account": account,
        "wf": wf,
        "total_enrolled": total_enrolled,
        "run_counts": run_counts,
        "step_breakdown": breakdown,
    })


@login_required
@module_required("automation")
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
@module_required("automation")
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
@module_required("automation")
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
@module_required("automation")
@require_POST
def workflow_delete(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    wf.delete()
    messages.success(request, "Workflow deleted.")
    return redirect("automation:list")

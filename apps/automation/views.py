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
from apps.automation import builder
from apps.automation.models import Workflow, WorkflowRun, WorkflowStepRun
from apps.automation.workflow_engine import validate_definition
from apps.automation.workflow_templates import (
    STARTER_TEMPLATES,
    STEP_CHANNEL,
    list_templates,
)
from apps.core.channels import enabled_channels
from apps.core.htmx import full_page_load
from apps.core.module_gate import module_required

# Ordered WhatsApp first: the editor lists them in this order and the first is what "Add step" offers.
_STEP_TYPES = [
    "reply_text",
    "send_buttons",
    "send_list",
    "wait_for_reply",
    "send_whatsapp",
    "create_lead",
    "update_lead_status",
    "assign_conversation",
    "notify_team",
    "add_tag",
    "remove_tag",
    "wait",
    "branch",
    "send_email",
    "webhook",
    "set_attribute",
    "stop",
]
_TRIGGER_TYPES = [
    "conversation.message_received",
    "contact.created",
    "lead.created",
    "lead.status_changed",
    "lead.qualified",
    "lead.lost",
    "contact.updated",
    "manual",
    "business_event",
    "email.opened",
    "email.clicked",
]


@login_required
@module_required("automations")
def workflow_list(request):
    from apps.automation import explain
    from apps.automation.labels import trigger_label

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    from django.db.models import Max, Q

    workflows = list(
        Workflow.objects.filter(account=account).annotate(
            last_ran=Max("runs__started_at"),
            helped=Count("runs__contact", distinct=True),
            failed_runs=Count("runs", filter=Q(runs__status="failed"), distinct=True),
            waiting_runs=Count("runs", filter=Q(runs__status="waiting"), distinct=True),
        )
    )
    for wf in workflows:
        # Computed here, not in the template: a dict without a "name" key makes
        # `{{ trigger.name }}` used as a *filter argument* raise
        # VariableDoesNotExist uncaught (unlike a plain variable, filter
        # arguments don't get the template engine's safe string_if_invalid
        # fallback) — crashed this page in production. See friendly_errors.py
        # note in apps.whatsapp for the same "translate once, in Python" rule.
        trigger = (wf.definition or {}).get("trigger") or {}
        wf.trigger_label = trigger_label(  # type: ignore[attr-defined]
            trigger.get("type", ""), trigger.get("name", "")
        )
        wf.summary = explain.explain_definition(wf.definition)  # type: ignore[attr-defined]
    groups, engagement, approved_templates = _engagement_starters(account, workflows)
    return render(
        request,
        "automation/workflow_list.html",
        {
            "account": account,
            "workflows": workflows,
            "starters": list_templates(),
            "engagement_starters": engagement,
            "goal_groups": groups,
            "approved_templates": approved_templates,
            "blank_choices": wa_variables_choices(),
            "pause_hour_choices": _PAUSE_HOUR_CHOICES,
            "template_data": {
                "business": account.company_name or "Your business",
                "templates": [
                    {"pk": t.pk, "content": t.content or "", "blanks": t.blanks}
                    for t in approved_templates
                ],
            },
        },
    )


def _no_room_redirect(request, account, slug):
    """A redirect with the reason when turning this automation on would pass the plan's limit,
    else None."""
    try:
        automation_api.ensure_room_to_turn_on(
            account, Workflow.objects.filter(account=account, slug=slug).first()
        )
    except automation_api.AutomationLimitReached as exc:
        messages.error(request, str(exc))
        return redirect("automation:list")
    return None


def wa_variables_choices():
    from apps.automation.variables import CHOICES

    return [{"key": key, "label": label} for key, _source, label, _fallback in CHOICES]


def _engagement_starters(account, workflows):
    """The one-click follow-up cards, each told whether it's already on.

    A card that can't work is said so plainly rather than installed and left to
    fail at send time: WhatsApp only delivers templates Meta has approved, so
    with no approved message there is nothing to send.
    """
    from apps.automation.engagement_starters import ENGAGEMENT_STARTERS, GOAL_GROUPS
    from apps.whatsapp.models import MessageTemplate

    approved = list(
        MessageTemplate.objects.filter(
            account=account,
            approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        ).order_by("name")
    )
    from apps.automation import variables as wa_variables

    # Each blank of each template, with the safest way to fill it already chosen.
    for template in approved:
        template.blanks = [  # type: ignore[attr-defined]
            {"name": name, "default": wa_variables.default_choice(name)}
            for name in (template.variables or [])
        ]
    installed = {
        wf.slug: wf for wf in workflows if wf.status == Workflow.Status.PUBLISHED
    }
    paused = {
        wf.slug: wf
        for wf in workflows
        if wf.status in (Workflow.Status.PAUSED, Workflow.Status.ARCHIVED)
    }
    cards = {
        starter["key"]: {
            **starter,
            "installed": installed.get(starter["key"]),
            "paused": paused.get(starter["key"]),
        }
        for starter in ENGAGEMENT_STARTERS
    }
    from apps.automation import readiness

    for card in cards.values():
        card["readiness"] = readiness.check(
            account, card, approved_template_count=len(approved)
        )
    groups = [
        {"title": title, "cards": [cards[key] for key in keys]}
        for title, keys in GOAL_GROUPS
    ]
    return groups, list(cards.values()), approved


@login_required
@module_required("automations")
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

    if starter.get("team"):
        return _install_team_starter(request, account, starter)
    if starter.get("menu"):
        return _install_menu_starter(request, account, starter)
    if starter.get("reply"):
        return _install_reply_starter(request, account, starter)
    if starter.get("qualification"):
        return _install_qualification_starter(request, account, starter)

    template = MessageTemplate.objects.filter(
        account=account,
        pk=request.POST.get("template_id") or 0,
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
    ).first()
    if template is None:
        messages.error(request, "Choose an approved message to send.")
        return redirect("automation:list")

    # Namespaced per template, like the conversation composer's template
    # picker: an x-show'd field is still submitted, so an unqualified name
    # could silently read a different template's stale value.
    from apps.automation import variables as wa_variables

    variable_mapping, variable_fallbacks = {}, {}
    for var in template.variables or []:
        key = f"{template.pk}__{var}"
        source, fallback = wa_variables.entry_from_choice(
            request.POST.get(f"var__{key}") or "",
            request.POST.get(f"lit__{key}") or "",
            request.POST.get(f"fb__{key}") or "",
        )
        if source:
            variable_mapping[var] = source
            if fallback:
                variable_fallbacks[var] = fallback
    missing = [var for var in (template.variables or []) if var not in variable_mapping]
    if missing:
        messages.error(request, "Fill in what should go in every blank of the message.")
        return redirect("automation:list")

    if (stop := _no_room_redirect(request, account, starter["key"])) is not None:
        return stop
    automation_api.upsert_published_workflow(
        account,
        slug=starter["key"],
        name=starter["name"],
        definition=build_definition(
            starter,
            template_name=template.whatsapp_template_name or "",
            variable_mapping=variable_mapping,
            variable_fallbacks=variable_fallbacks,
        ),
    )
    messages.success(request, f"{starter['name']} is on. {starter['stop_condition']}")
    return redirect("automation:list")


_MAX_REPLY_LENGTH = 1000
_MENU_OPTIONS = 3


def _install_qualification_starter(request, account, starter):
    """Install "qualify new leads": fixed questions, so there's nothing to choose but "on"."""
    from apps.automation.engagement_starters import (
        install_lead_qualification_questionnaire,
    )

    if (stop := _no_room_redirect(request, account, starter["key"])) is not None:
        return stop
    install_lead_qualification_questionnaire(account)
    messages.success(request, f"{starter['name']} is on. {starter['stop_condition']}")
    return redirect("automation:list")


def _install_team_starter(request, account, starter):
    """Install "hand new leads to your team": the owner's message, who to tell, and whether to assign."""
    from apps.automation.engagement_starters import build_team_definition

    text = (request.POST.get("notify_text") or "").strip()
    if not text:
        messages.error(request, "Write what your team should be told.")
        return redirect("automation:list")
    if len(text) > 1000:
        messages.error(request, "Keep the message under 1000 characters.")
        return redirect("automation:list")
    notify = "owners" if request.POST.get("notify_to") == "owners" else "assignee"
    if (stop := _no_room_redirect(request, account, starter["key"])) is not None:
        return stop
    automation_api.upsert_published_workflow(
        account,
        slug=starter["key"],
        name=starter["name"],
        definition=build_team_definition(
            starter, text=text, notify=notify, assign=request.POST.get("assign") == "on"
        ),
    )
    messages.success(request, f"{starter['name']} is on. {starter['stop_condition']}")
    return redirect("automation:list")


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
            messages.error(
                request, f"Option {n} needs both a button label and an answer."
            )
            return redirect("automation:list")
        if len(reply) > _MAX_REPLY_LENGTH:
            messages.error(
                request,
                f"Keep option {n}'s answer under {_MAX_REPLY_LENGTH} characters.",
            )
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
    if (stop := _no_room_redirect(request, account, starter["key"])) is not None:
        return stop
    automation_api.upsert_published_workflow(
        account,
        slug=starter["key"],
        name=starter["name"],
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
    keywords = None
    if starter.get("keywords") and request.POST.get("keywords") is not None:
        keywords = _clean_keywords(request.POST.get("keywords", ""))
        if not keywords:
            messages.error(request, "Add at least one word for Akilent to listen for.")
            return redirect("automation:list")
    # The "then" checkboxes: an old form without them keeps the starter's default (tag on).
    from_recipe = request.POST.get("then_present") == "1"
    if (stop := _no_room_redirect(request, account, starter["key"])) is not None:
        return stop
    automation_api.upsert_published_workflow(
        account,
        slug=starter["key"],
        name=starter["name"],
        definition=build_reply_definition(
            starter,
            text=text,
            keywords=keywords,
            tag=(request.POST.get("add_tag") == "on") if from_recipe else True,
            notify=request.POST.get("tell_team") == "on",
        ),
    )
    messages.success(request, f"{starter['name']} is on. {starter['stop_condition']}")
    return redirect("automation:list")


_MAX_KEYWORDS = 12


def _clean_keywords(raw: str) -> list[str]:
    """Words typed as "price, cost, how much": trimmed, de-duplicated, short, and not too many."""
    seen, words = set(), []
    for part in raw.replace("\n", ",").split(","):
        word = part.strip()[:40]
        if word and word.casefold() not in seen:
            seen.add(word.casefold())
            words.append(word)
    return words[:_MAX_KEYWORDS]


@login_required
@module_required("automations")
@require_POST
def starter_test(request):
    """ "Send test to me": the reply the owner is setting up, sent to their own WhatsApp.

    Goes through the normal send path, so WhatsApp's rules apply: a normal message can only be
    sent to a number that wrote to the business in the last 24 hours. We say so plainly instead
    of creating a contact or faking consent.
    """
    from apps.automation.engagement_starters import STARTERS_BY_KEY
    from apps.automation.variables import merge_first_name
    from apps.whatsapp import api as whatsapp_api

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    starter = STARTERS_BY_KEY.get(request.POST.get("starter", ""))
    if (
        starter is None
        or not starter.get("reply")
        or starter.get("menu")
        or starter.get("team")
    ):
        messages.error(request, "Test sending is available for the reply automations.")
        return redirect("automation:list")
    text = (request.POST.get("reply_text") or "").strip()
    if not text:
        messages.error(request, "Write the reply first, then send yourself a test.")
        return redirect("automation:list")
    contact = whatsapp_api.find_contact_by_phone(
        account, request.POST.get("test_phone", "")
    )
    if contact is None or not whatsapp_api.free_text_window_is_open(contact):
        messages.error(
            request,
            "To send you a test, WhatsApp needs you to have messaged your business number from that "
            "phone in the last 24 hours. Send it a quick message from your phone, then try again.",
        )
        return redirect("automation:list")
    first = contact.contact.first_name if contact.contact_id and contact.contact else ""
    from apps.automation.variables import merge_business_facts

    whatsapp_api.send_message(
        account,
        contact,
        merge_business_facts(merge_first_name(text, first), account),
        sent_by="system",  # a test to the owner's own phone, not a reply to a customer
    )
    messages.success(
        request,
        "Test sent to your WhatsApp. This is exactly the message Akilent will send when this automation runs.",
    )
    return redirect("automation:list")


@login_required
@module_required("automations")
@require_POST
def workflow_create(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    starter = request.POST.get("from_template") or ""
    name = (request.POST.get("name") or "").strip()
    # A new automation starts from a WhatsApp moment (a customer messages you), not a manual run.
    definition: dict = {
        "trigger": {"type": "conversation.message_received"},
        "steps": [{"id": "stop", "type": "stop"}],
    }
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
    wf = Workflow.objects.create(
        account=account, name=name, slug=slug, definition=definition
    )
    return redirect("automation:editor", slug=wf.slug)


def _followup_owners(account) -> list:
    """Teammates a follow-up step can be for, as ``(email, name)``: the step stores the email,
    which is what the engine resolves (``assign_to``)."""
    from apps.accounts.models import Membership

    return [
        (m.user.email, m.user.get_full_name() or m.user.email)
        for m in Membership.objects.filter(account=account, user__is_active=True)
        .select_related("user")
        .order_by("user__first_name", "user__email")
        if m.user.email
    ]


@login_required
@module_required("automations")
@full_page_load  # rich editor: its scripts expect a fresh page
def workflow_editor(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)

    from apps.email.models import EmailTemplate
    from apps.whatsapp.models import MessageTemplate

    email_templates = [
        {"slug": t.slug, "name": t.name, "subject": t.subject}
        for t in EmailTemplate.objects.filter(account=account, is_active=True).order_by(
            "name"
        )
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

    from apps.contacts.models import Contact, CustomAttributeDef
    from apps.conversations.models import ConversationForm
    from apps.core.actions import available_actions

    return render(
        request,
        "automation/workflow_editor.html",
        {
            "account": account,
            "wf": wf,
            "definition_json": json.dumps(
                automation_api.editable_definition(wf), indent=2
            ),
            "has_changes": wf.draft_definition is not None,
            # Existing steps keep working; only new ones are offered for enabled channels.
            "step_types": json.dumps(
                [
                    t
                    for t in _STEP_TYPES
                    if STEP_CHANNEL.get(t) in (None, *enabled_channels())
                ]
            ),
            "trigger_types": json.dumps(_TRIGGER_TYPES),
            "step_catalog_json": json.dumps(builder.step_catalog(account)),
            "trigger_catalog_json": json.dumps(builder.trigger_catalog()),
            "branch_fields_json": json.dumps(builder.BRANCH_FIELDS),
            "attribute_keys": sorted(
                CustomAttributeDef.objects.filter(account=account)
                .exclude(archived_at__isnull=False)
                .values_list("key", flat=True)
                .distinct()
            ),
            "action_names": available_actions(),
            "forms_json": json.dumps(
                [
                    {
                        "id": f.public_id,
                        "name": f.name,
                        "ready": f.status == "published",
                    }
                    for f in ConversationForm.objects.filter(account=account).order_by(
                        "name"
                    )
                ]
            ),
            "followup_owners": (owners := _followup_owners(account)),
            "followup_owner_emails_json": json.dumps([email for email, _ in owners]),
            "test_contacts": [
                {"id": c.public_id, "label": c.display_name}
                for c in Contact.objects.filter(account=account).order_by(
                    "-last_engaged_at", "-id"
                )[:25]
            ],
            "validation_errors": json.dumps(
                validate_definition(
                    automation_api.editable_definition(wf), account=account
                )
            ),
            "email_templates_json": json.dumps(email_templates),
            "whatsapp_templates_json": json.dumps(whatsapp_templates),
        },
    )


@login_required
@module_required("automations")
def workflow_stats(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)

    run_counts = {
        row["status"]: row["count"]
        for row in WorkflowRun.objects.filter(workflow=wf)
        .values("status")
        .annotate(count=Count("id"))
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
        entry = steps_by_id.setdefault(
            row["step_id"],
            {
                "step_id": row["step_id"],
                "step_type": row["step_type"],
                "ok": 0,
                "error": 0,
            },
        )
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

    return render(
        request,
        "automation/workflow_stats.html",
        {
            "account": account,
            "wf": wf,
            "total_enrolled": total_enrolled,
            "run_counts": run_counts,
            "step_breakdown": breakdown,
            "ai_quality": automation_api.ai_quality(wf),
        },
    )


@login_required
@module_required("automations")
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
        wf.save(update_fields=["name", "updated_at"])
    if isinstance(body.get("priority"), int) and 1 <= body["priority"] <= 999:
        wf.priority = body["priority"]
        wf.save(update_fields=["priority", "updated_at"])
    if isinstance(body.get("definition"), dict):
        # A workflow that's on keeps running its published steps; edits wait here until
        # "Publish changes", so a half-built step never reaches a customer.
        automation_api.save_edits(wf, body["definition"])
    editing = automation_api.editable_definition(wf)
    return JsonResponse(
        {
            "ok": True,
            "errors": validate_definition(editing, account=account),
            "status": wf.status,
            "version": wf.version,
            "has_changes": wf.draft_definition is not None,
        }
    )


_PAUSE_HOUR_CHOICES = (1, 2, 4, 12, 24, 72)


@login_required
@module_required("automations")
@require_POST
def automation_settings(request):
    """Business-wide automation settings (owners and admins)."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if not _can_switch(request, account):
        messages.error(request, "Only an owner or admin can change this.")
        return redirect("automation:list")
    try:
        hours = int(request.POST.get("pause_automation_hours", "0"))
    except ValueError:
        hours = 0
    account.pause_automation_hours = hours if hours in _PAUSE_HOUR_CHOICES else 0
    account.save(update_fields=["pause_automation_hours"])
    messages.success(
        request,
        f"Automations will step back for {hours} hours after a teammate replies."
        if account.pause_automation_hours
        else "Automations keep answering after a teammate replies.",
    )
    return redirect("automation:list")


@login_required
@module_required("automations")
@require_POST
def workflow_validate(request, slug: str):
    """Check the editor's current work without saving it: problems plus a plain summary."""
    from apps.automation import explain

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)
    get_object_or_404(Workflow, account=account, slug=slug)
    try:
        definition = json.loads(request.body or "{}").get("definition")
    except (ValueError, AttributeError):
        return JsonResponse({"error": "invalid JSON"}, status=400)
    if not isinstance(definition, dict):
        return JsonResponse({"error": "definition must be an object"}, status=400)
    try:
        summary = explain.explain_definition(definition)
    except Exception:
        summary = None  # a half-built step must never stop the editor checking the rest
    return JsonResponse(
        {
            "errors": validate_definition(definition, account=account)
            + automation_api.fact_warnings(account, definition),
            "summary": summary,
        }
    )


@login_required
@module_required("automations")
@require_POST
def workflow_coverage(request, slug: str):
    """Try realistic customer messages against the trigger before publishing."""
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        definition = automation_api.editable_definition(wf)
    phrases = [str(p)[:200] for p in (body.get("phrases") or []) if str(p).strip()][:10]
    return JsonResponse(
        automation_api.coverage(account, wf, definition, extra_phrases=phrases)
    )


@login_required
@module_required("automations")
@require_POST
def workflow_simulate(request, slug: str):
    """Play the editor's current steps against a test message. Sends and changes nothing."""
    from apps.automation import simulate
    from apps.contacts.models import Contact

    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    try:
        body = json.loads(request.body or "{}")
    except ValueError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    definition = body.get("definition")
    if not isinstance(definition, dict):
        definition = automation_api.editable_definition(wf)
    contact = None
    if body.get("contact"):
        contact = Contact.objects.filter(
            account=account, public_id=str(body["contact"])
        ).first()
    replies = [str(r)[:1000] for r in (body.get("replies") or [])][:20]
    return JsonResponse(
        simulate.simulate(
            account,
            definition,
            contact=contact,
            message=str(body.get("message") or "")[:1000],
            replies=replies,
        )
    )


@login_required
@module_required("automations")
@require_POST
def workflow_enroll(request, slug: str):
    """Start a hand-run automation for one customer (or several, from a list)."""
    from django.utils.http import url_has_allowed_host_and_scheme

    from apps.contacts.models import Contact

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(
        Workflow, account=account, slug=slug, status=Workflow.Status.PUBLISHED
    )
    ids = request.POST.getlist("contact")[:500]
    contacts = list(Contact.objects.filter(account=account, public_id__in=ids))
    context = {}
    if request.POST.get("conversation"):
        context["conversation_id"] = request.POST["conversation"][:40]
    counts = automation_api.enroll_manually(wf, contacts, context=context)
    parts = []
    if counts["started"]:
        parts.append(f"Started {wf.name} for {counts['started']}")
    if counts["already"]:
        parts.append(f"{counts['already']} already part-way through it")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} couldn't start (opted out, or not allowed)")
    (messages.success if counts["started"] else messages.warning)(
        request, ". ".join(parts) + "." if parts else "Nobody was chosen."
    )
    nxt = request.POST.get("next") or ""
    if url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect("automation:runs", slug=wf.slug)


def _can_switch(request, account) -> bool:
    """Turning an automation on or off changes what every customer gets: owners and admins only."""
    from apps.accounts.api import is_account_admin

    return request.user.is_superuser or is_account_admin(request.user, account)


def _not_allowed(request, wf):
    messages.error(
        request,
        "Only an owner or admin can turn automations on or off. Ask one of them.",
    )
    return redirect("automation:editor", slug=wf.slug)


@login_required
@module_required("automations")
@require_POST
def workflow_publish(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    if not _can_switch(request, account):
        return _not_allowed(request, wf)
    try:
        automation_api.publish(wf)
    except (
        automation_api.WorkflowNotReady,
        automation_api.AutomationLimitReached,
    ) as exc:
        messages.error(request, f"Fix this before publishing: {exc}")
        return redirect("automation:editor", slug=wf.slug)
    messages.success(
        request,
        f"Published {wf.name} (v{wf.version}). Customers already part-way through "
        "finish on the version they started.",
    )
    return redirect("automation:editor", slug=wf.slug)


@login_required
@module_required("automations")
@require_POST
def workflow_discard(request, slug: str):
    """Throw away unpublished edits; the automation keeps running as it was."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    automation_api.discard_edits(wf)
    messages.success(request, "Your changes were discarded.")
    return redirect("automation:editor", slug=wf.slug)


@login_required
@module_required("automations")
@require_POST
def workflow_archive(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    if not _can_switch(request, account):
        return _not_allowed(request, wf)
    ended = automation_api.archive(wf)
    messages.success(
        request,
        f"Archived {wf.name}."
        + (
            f" {ended} customer{'s' if ended != 1 else ''} part-way through were let go."
            if ended
            else ""
        ),
    )
    return redirect("automation:list")


@login_required
@module_required("automations")
@require_POST
def workflow_pause(request, slug: str):
    """Pause an automation: nothing new starts, and customers part-way through are held."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    if not _can_switch(request, account):
        return _not_allowed(request, wf)
    automation_api.pause(wf)
    messages.success(
        request,
        f"{wf.name} is paused. Customers part-way through are held and carry on when "
        "you turn it back on.",
    )
    return redirect("automation:list")


@login_required
@module_required("automations")
@require_POST
def workflow_resume(request, slug: str):
    """Turn a paused automation back on, exactly as it was (no new version)."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    if not _can_switch(request, account):
        return _not_allowed(request, wf)
    waiting = "cancel" if request.POST.get("waiting") == "cancel" else "continue"
    try:
        automation_api.resume(wf, waiting=waiting)
    except (
        automation_api.WorkflowNotReady,
        automation_api.AutomationLimitReached,
    ) as exc:
        messages.error(request, f"{wf.name} can't be turned on yet: {exc}")
        return redirect("automation:list")
    messages.success(request, f"{wf.name} is on.")
    return redirect("automation:list")


@login_required
@module_required("automations")
@require_POST
def workflow_delete(request, slug: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    if not _can_switch(request, account):
        return _not_allowed(request, wf)
    wf.delete()
    messages.success(request, f"{wf.name} was removed.")
    return redirect("automation:list")


_RUN_FILTERS = {
    "failed": ["failed"],
    "waiting": ["waiting", "active"],
    "done": ["completed"],
    "cancelled": ["cancelled"],
}


@login_required
@module_required("automations")
def workflow_runs(request, slug: str):
    """Every customer who went through this automation, newest first, filterable."""
    from django.core.paginator import Paginator
    from django.db.models import Q

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    wf = get_object_or_404(Workflow, account=account, slug=slug)
    status = request.GET.get("status", "")
    q = (request.GET.get("q") or "").strip()
    runs = wf.runs.select_related("contact").order_by("-started_at", "-id")
    if status in _RUN_FILTERS:
        runs = runs.filter(status__in=_RUN_FILTERS[status])
    if q:
        runs = runs.filter(
            Q(contact__first_name__icontains=q)
            | Q(contact__last_name__icontains=q)
            | Q(contact__phone__icontains=q)
            | Q(contact__email__icontains=q)
        )
    page = Paginator(runs, 25).get_page(request.GET.get("page"))
    counts = {
        key: wf.runs.filter(status__in=values).count()
        for key, values in _RUN_FILTERS.items()
    }
    return render(
        request,
        "automation/workflow_runs.html",
        {"wf": wf, "page": page, "status": status, "q": q, "counts": counts},
    )


@login_required
@module_required("automations")
def run_detail(request, run_id: str):
    """One customer's path through an automation, with ways to fix a failure."""
    from apps.conversations.models import Conversation

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    run = get_object_or_404(
        WorkflowRun.objects.select_related("workflow", "contact", "version"),
        public_id=run_id,
        workflow__account=account,
    )
    conversation = None
    if (run.context or {}).get("conversation_id"):
        conversation = Conversation.objects.filter(
            account=account, public_id=run.context["conversation_id"]
        ).first()
    return render(
        request,
        "automation/run_detail.html",
        {
            "run": run,
            "wf": run.workflow,
            "timeline": automation_api.run_timeline(run),
            "conversation": conversation,
            "version_number": run.version.number if run.version else None,
        },
    )


@login_required
@module_required("automations")
@require_POST
def run_action(request, run_id: str, action: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    run = get_object_or_404(
        WorkflowRun.objects.select_related("workflow"),
        public_id=run_id,
        workflow__account=account,
    )
    if action == "correct":
        from apps.automation.models import WorkflowStepRun

        step_run = get_object_or_404(
            WorkflowStepRun, run=run, pk=request.POST.get("step_run") or 0
        )
        try:
            automation_api.correct_ai_label(step_run, request.POST.get("label", ""))
        except automation_api.RunActionError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request, "Thanks. It will learn from this for the next messages."
            )
        return redirect("automation:run", run_id=run.public_id)
    handlers = {
        "retry": (automation_api.retry_run, "Tried that step again."),
        "skip": (automation_api.skip_failed_step, "Skipped that step and carried on."),
        "cancel": (automation_api.cancel_run, "Stopped this run."),
    }
    if action not in handlers:
        return redirect("automation:run", run_id=run.public_id)
    handler, done = handlers[action]
    try:
        run = handler(run)
    except automation_api.RunActionError as exc:
        messages.error(request, str(exc))
        return redirect("automation:run", run_id=run.public_id)
    if action == "retry" and run.status == WorkflowRun.Status.FAILED:
        messages.error(request, "It failed again. See the reason below.")
    else:
        messages.success(request, done)
    return redirect("automation:run", run_id=run.public_id)


@login_required
@module_required("automations")
def why_not(request):
    """ "Why didn't it reply?": what each automation did with a customer's latest message, in plain words.

    Defaults to the most recent customer message in the business; ``?conversation=`` picks another.
    """
    from datetime import timedelta

    from django.utils import timezone

    from apps.automation import explain
    from apps.automation.workflow_engine import explain_enrollment
    from apps.conversations import attribution
    from apps.conversations.models import Message
    from apps.whatsapp import api as whatsapp_api

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    inbound = Message.objects.filter(
        account=account, direction=Message.Direction.INBOUND
    )
    public_id = (request.GET.get("conversation") or "").strip()
    latest = None
    if public_id:
        latest = (
            inbound.filter(conversation__public_id=public_id)
            .select_related("conversation__contact")
            .order_by("-timestamp")
            .first()
        )
    if latest is None and not public_id:
        latest = (
            inbound.select_related("conversation__contact")
            .order_by("-timestamp")
            .first()
        )

    ctx = {
        "account": account,
        "message": None,
        "verdicts": [],
        "notes": [],
        "choices": attribution.recent_choices(account),
    }
    if latest is not None:
        conversation, contact = latest.conversation, latest.conversation.contact
        reply = (latest.metadata or {}).get("reply") or {}
        message = {
            "body": latest.body,
            "reply_id": reply.get("id", ""),
            "reply_title": reply.get("title", ""),
        }
        earlier = inbound.filter(
            conversation__contact=contact, timestamp__lt=latest.timestamp
        ).exists()
        verdicts = explain_enrollment(
            account.id,
            contact,
            message=message,
            message_at=latest.timestamp,
            is_first_message=not earlier,
        )
        notes = []
        from apps.billing import api as billing_api

        if not whatsapp_api.automations_start_from_messages():
            notes.append(
                "Automations are switched off for the whole site, so no customer message starts one, "
                "however it is set up. This is a platform setting, not a mistake in your automation. "
                'Ask your Akilent administrator to turn on "automation events".'
            )
        if not billing_api.usable(account, "automations"):
            notes.append("Automations aren't included in your plan.")
        if getattr(contact, "whatsapp_opted_out", False):
            notes.append(
                "This customer has opted out of messages, so no automation will send to them."
            )
        if timezone.now() - latest.timestamp > timedelta(hours=24):
            notes.append(
                "It has been more than 24 hours since this message, so only an approved message can be sent now."
            )
        ctx.update(
            {
                "message": latest,
                "conversation": conversation,
                "contact": contact,
                "verdicts": [explain.describe_verdict(v) for v in verdicts],
                "notes": notes,
                # Customers ask this often and the team answers by hand: offer to automate it.
                "automate_offer": automation_api.repeated_question_offer(
                    account, latest.body
                ),
            }
        )
    return render(request, "automation/why_not.html", ctx)

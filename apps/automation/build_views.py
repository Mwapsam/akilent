"""Build: where a business sets Akilent up after connecting WhatsApp.

Step 1 "Tell us about your business" (profile + opening hours), step 2 "Bring in what you have"
(templates from Meta), step 3 "Three things to automate" (``recommendations``), plus "describe
something else" and "create a template" when AI is on. Every automation, whatever proposed it, is
built by ``intents.build_from_intent`` and shown on the review page before anything is saved.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.automation import intents, patterns, recommendations
from apps.core.module_gate import module_required

STEPS = ((1, "Tell us about your business"), (2, "Bring in what you have"), (3, "Three things to automate"))


def _ai_on(account) -> bool:
    from apps.ai import api as ai_api

    return ai_api.is_available(account)


def _template_page_url() -> str:
    """The WhatsApp template page, or "" on a site where WhatsApp isn't switched on."""
    from django.urls import NoReverseMatch

    try:
        return reverse("whatsapp-template-create")
    except NoReverseMatch:
        return ""


@login_required
@module_required("automations")
def build_home(request):
    from apps.accounts import business_hours as bh
    from apps.accounts import profile as business_profile
    from apps.whatsapp import api as whatsapp_api

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    profile = business_profile.get_profile(account)
    try:
        step = int(request.GET.get("step") or 0)
    except ValueError:
        step = 0
    if step not in (1, 2, 3):
        step = 3 if profile and profile.completed_at else 1
    hours = bh.get_hours(account)
    ctx = {
        "account": account, "step": step, "steps": STEPS, "profile": profile,
        "payment_choices": business_profile.PAYMENT_METHODS.items(),
        "rows": bh.form_rows(hours), "timezones": bh.timezone_choices(),
        "current_tz": hours.timezone if hours else "Africa/Lusaka", "can_edit": True,
        "ai_on": _ai_on(account), "template_url": _template_page_url(),
    }
    if step == 2:
        ctx["template_counts"] = whatsapp_api.template_counts(account)
        facts = business_profile.as_facts(account)
        ctx["starter_tags"] = [t for t, needed in (
            ("asked-prices", True), ("asked-location", facts.get("location")), ("asked-hours", bh.is_configured(account)),
            ("asked-delivery", facts.get("delivery")), ("asked-payment", facts.get("payment_methods")),
            ("welcomed", True)) if needed]
    if step == 3:
        ctx["cards"] = recommendations.recommend(account)
    return render(request, "automation/build.html", ctx)


@login_required
@module_required("automations")
@require_POST
def build_profile(request):
    """Save step 1: the business profile and opening hours, then start the AI notes from them."""
    from apps.accounts import business_hours as bh
    from apps.accounts import profile as business_profile
    from apps.ai import api as ai_api

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    delivers = {"yes": True, "no": False}.get(request.POST.get("delivers", ""))
    try:
        business_profile.save_profile(
            account, what_you_sell=request.POST.get("what_you_sell", ""), location=request.POST.get("location", ""),
            delivers=delivers, delivery_notes=request.POST.get("delivery_notes", ""),
            payment_methods=request.POST.getlist("payment_methods"), payment_other=request.POST.get("payment_other", ""),
            website=request.POST.get("website", ""),
        )
        if request.POST.get("set_hours") == "on":
            bh.save_hours(account, tz=request.POST.get("timezone", ""), schedule=bh.schedule_from_form(request.POST))
    except (business_profile.ProfileError, bh.HoursError) as exc:
        messages.error(request, str(exc))
        return redirect(reverse("build:home") + "?step=1")
    ai_api.seed_notes(account, business_profile.as_notes(account))
    messages.success(request, "Saved. Akilent will use these answers in replies and templates.")
    return redirect(reverse("build:home") + "?step=2")


@login_required
@module_required("automations")
@require_POST
def build_import_templates(request):
    from apps.whatsapp import api as whatsapp_api

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    try:
        result = whatsapp_api.import_templates(account)
    except Exception:  # noqa: BLE001 - the button must explain, never 500
        result = {"synced": 0, "errors": ["WhatsApp couldn't be reached."]}
    errors = result.get("errors") or []
    if errors:
        messages.error(request, "Couldn't bring in all your templates: " + str(errors[0]))
    else:
        messages.success(request, f"Brought in {result.get('synced', 0)} templates from WhatsApp.")
    return redirect(reverse("build:home") + "?step=2")


@login_required
@module_required("automations")
@require_POST
def build_dismiss(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    key = request.POST.get("key", "")
    if key.startswith(("intent:", "pattern:")):
        patterns.dismiss(account, key)
        messages.info(request, "Hidden for 30 days.")
    return redirect(request.POST.get("next") or reverse("build:home") + "?step=3")


def _source(account, params) -> tuple[str, dict, dict, object]:
    """``(intent, entities, evidence, draft)`` from ?intent= / ?pattern= / ?draft=."""
    from apps.ai import api as ai_api
    from apps.automation.models import ReplyPattern

    if params.get("pattern"):
        pattern = ReplyPattern.objects.filter(account=account, key=params["pattern"]).first()
        if pattern is None:
            raise intents.IntentError("That suggestion is no longer available.")
        return ("answer_question", {"keywords": pattern.question_keywords, "reply_text": pattern.reply_text,
                                    "topic_label": pattern.topic}, {"team_reply_count": pattern.count}, None)
    if params.get("draft"):
        draft = ai_api.get_draft(account, params["draft"], kind="automation")
        if draft is None or draft.status not in ("ready", "used"):
            raise intents.IntentError("That draft isn't ready.")
        return draft.result.get("intent", ""), draft.result.get("entities") or {}, {}, draft
    return params.get("intent", ""), {}, {}, None


@login_required
@module_required("automations")
def build_review(request):
    """Show exactly what an automation will do before it's saved, then save it or turn it on."""
    from apps.ai import api as ai_api
    from apps.automation import api as automation_api
    from apps.automation import explain

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    params = request.POST if request.method == "POST" else request.GET
    try:
        intent, entities, evidence, draft = _source(account, params)
    except intents.IntentError as exc:
        messages.error(request, str(exc))
        return redirect(reverse("build:home") + "?step=3")
    if request.method == "POST":
        if params.get("reply_text") is not None:
            entities["reply_text"] = params.get("reply_text", "")
        if params.get("keywords") is not None:
            entities["keywords"] = [w.strip() for w in params.get("keywords", "").split(",") if w.strip()]
        if params.get("tag") is not None:
            entities["tag"] = params.get("tag", "").strip()
    try:
        built = intents.build_from_intent(account, intent, entities, evidence=evidence)
    except intents.IntentError as exc:
        messages.error(request, str(exc))
        if request.method == "POST":
            built = None
        else:
            return redirect(reverse("build:home") + "?step=3")

    action = params.get("action", "") if request.method == "POST" else ""
    if built and action in ("save", "turn_on") and not (action == "turn_on" and built["errors"]):
        try:
            workflow = automation_api.save_built_workflow(
                account, slug=built["slug"], name=built["name"], definition=built["definition"],
                turn_on=action == "turn_on")
        except automation_api.WorkflowNotReady as exc:
            messages.error(request, f"It can't be turned on yet: {exc}")
        else:
            ai_api.mark_draft_used(draft)
            if params.get("pattern"):
                patterns.dismiss(account, f"pattern:{params['pattern']}", days=365)
            messages.success(request, f"{workflow.name} is on." if action == "turn_on"
                             else f"Saved {workflow.name} as a draft. Turn it on from Automations when you're ready.")
            return redirect("automation:list")

    return render(request, "automation/build_review.html", {
        "account": account, "built": built, "intent": intent,
        "summary": explain.explain_definition(built["definition"]) if built else None,
        "source": {k: params.get(k, "") for k in ("intent", "pattern", "draft") if params.get(k)},
        "keywords_text": ", ".join(built["keywords"]) if built else "",
    })


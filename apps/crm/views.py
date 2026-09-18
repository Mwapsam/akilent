"""Sales surface: a thin projection over Lead/Deal, built to the same UX
rules as Inbox — one primary action per screen, business language ("Sales"),
teaching empty states.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.utils import get_current_account
from apps.core.actions import ActionError, run_action
from apps.crm.models import Deal, Lead, Pipeline


@login_required
def sales(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    leads = Lead.objects.filter(
        account=account, status__in=[Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED],
    ).select_related("contact")

    pipeline = Pipeline.objects.filter(account=account, is_default=True).first()
    stages = list(pipeline.stages.all()) if pipeline else []
    for stage in stages:
        # Attached here (rather than a template dict-lookup filter) so the
        # template can just do {% for deal in stage.deals_in_stage %}.
        stage.deals_in_stage = list(
            Deal.objects.filter(account=account, stage=stage).select_related("contact")
        )

    return render(request, "crm/sales.html", {
        "account": account,
        "leads": leads,
        "stages": stages,
        "has_pipeline": pipeline is not None,
    })


@login_required
def lead_detail(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    lead = get_object_or_404(Lead, account=account, public_id=public_id)

    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "convert":
                result = run_action(
                    "create_deal", {"account": account}, lead=lead,
                    title=request.POST.get("title") or None,
                    value=request.POST.get("value") or 0,
                )
                messages.success(request, "Lead converted to a deal.")
                return redirect("crm:deal-detail", public_id=result["deal_id"])
        except ActionError as exc:
            messages.error(request, str(exc))
        return redirect("crm:lead-detail", public_id=public_id)

    return render(request, "crm/lead_detail.html", {"account": account, "lead": lead})


@login_required
def deal_detail(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    deal = get_object_or_404(Deal, account=account, public_id=public_id)

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "move_stage":
            stage_id = request.POST.get("stage_id")
            stage = get_object_or_404(deal.pipeline.stages, id=stage_id)
            try:
                run_action("change_deal_stage", {"account": account}, deal=deal, stage=stage)
            except ActionError as exc:
                messages.error(request, str(exc))
        return redirect("crm:deal-detail", public_id=public_id)

    return render(request, "crm/deal_detail.html", {
        "account": account, "deal": deal, "stages": deal.pipeline.stages.all(),
    })

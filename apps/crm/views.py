"""Interested-customers surface: a thin projection over Lead/Deal, built to the
same UX rules as Inbox — one primary action per screen, plain language (the
words on screen are "interested customers", not "leads" and "deals"),
teaching empty states.
"""
from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from apps.accounts.utils import get_current_account
from apps.contacts.models import Contact
from apps.core.actions import ActionError, run_action
from apps.core.module_gate import module_required
from apps.crm.models import Deal, Lead, Pipeline


@login_required
@module_required("crm")
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
@module_required("crm")
def create_lead_view(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    if request.method == "POST":
        # A conversation's "Create lead" button sets this so the agent lands back
        # in the conversation with a confirmation, instead of being pulled into
        # Sales mid-reply (see docs/plans R1.5a UX review).
        next_url = request.POST.get("next") or None

        query = (request.POST.get("contact") or "").strip()
        contact = Contact.objects.filter(account=account).filter(
            Q(phone=query) | Q(email__iexact=query)
        ).first()
        if contact is None:
            messages.error(
                request,
                "No customer found with that phone or email. Check the number/email, "
                "or add them from Contacts first.",
            )
            return redirect(next_url or "crm:sales")

        try:
            result = run_action(
                "create_lead", {"account": account}, account=account, contact=contact,
                source=(request.POST.get("source") or "manual").strip(),
            )
        except ActionError as exc:
            messages.error(request, str(exc))
            return redirect(next_url or "crm:sales")

        value = (request.POST.get("value") or "").strip()
        if value:
            lead = Lead.objects.get(public_id=result["lead_id"])
            try:
                deal_result = run_action(
                    "create_deal", {"account": account}, lead=lead, value=value,
                )
                messages.success(request, "Lead created and moved into your pipeline.")
                return redirect(next_url) if next_url else redirect(
                    "crm:deal-detail", public_id=deal_result["deal_id"]
                )
            except ActionError as exc:
                messages.error(request, str(exc))

        messages.success(request, "Lead created.")
        return redirect(next_url) if next_url else redirect("crm:lead-detail", public_id=result["lead_id"])

    return redirect("crm:sales")


@login_required
@module_required("crm")
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
@module_required("crm")
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

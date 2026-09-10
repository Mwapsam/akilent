"""Dashboard: the "Scheduled" section — everything queued to fire later,
grouped by day, with Cancel / Reschedule / View-target actions.
"""
from __future__ import annotations

import json
from collections import OrderedDict

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.scheduler.api import SchedulingError, cancel_job, reschedule_job
from apps.scheduler.models import ScheduledJob


@login_required
def scheduled_index(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    qs = (
        ScheduledJob.objects.filter(account=account, parent__isnull=True)
        .select_related("target_campaign", "target_message")
        .order_by("fire_at", "id")
    )
    show = (request.GET.get("show") or "upcoming").strip()
    if show == "upcoming":
        qs = qs.filter(status__in=[
            ScheduledJob.Status.SCHEDULED,
            ScheduledJob.Status.PROCESSING,
            ScheduledJob.Status.RUNNING,
        ])
    elif show in {c for c, _ in ScheduledJob.Status.choices}:
        qs = qs.filter(status=show)

    groups: "OrderedDict[str, list]" = OrderedDict()
    for job in qs[:500]:
        day = timezone.localtime(job.fire_at).strftime("%A, %B %d").replace(" 0", " ")
        groups.setdefault(day, []).append(job)

    return render(request, "scheduler/index.html", {
        "account": account,
        "groups": groups,
        "show": show,
        "status_choices": ScheduledJob.Status.choices,
        "total": qs.count(),
    })


def _job_or_none(account, public_id):
    return ScheduledJob.objects.filter(
        account=account, public_id=public_id, parent__isnull=True
    ).first()


@login_required
@require_POST
def scheduled_cancel(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)
    job = _job_or_none(account, public_id)
    if job is None:
        return JsonResponse({"error": "not found"}, status=404)
    try:
        cancel_job(job)
    except SchedulingError as exc:
        return JsonResponse({"error": str(exc)}, status=409)
    return JsonResponse({"status": job.status})


@login_required
@require_POST
def scheduled_reschedule(request, public_id: str):
    account = get_current_account(request)
    if account is None:
        return JsonResponse({"error": "no account"}, status=403)
    job = _job_or_none(account, public_id)
    if job is None:
        return JsonResponse({"error": "not found"}, status=404)
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid JSON"}, status=400)
    scheduled_at = parse_datetime(str(body.get("scheduled_at") or ""))
    if scheduled_at is None:
        return JsonResponse({"error": "scheduled_at is required (ISO 8601)"}, status=400)
    try:
        reschedule_job(job, scheduled_at=scheduled_at, tz=body.get("timezone") or "")
    except SchedulingError as exc:
        code = 409 if job.status != ScheduledJob.Status.SCHEDULED else 400
        return JsonResponse({"error": str(exc)}, status=code)
    return JsonResponse({"status": job.status, "fire_at": job.fire_at.isoformat()})

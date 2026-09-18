"""Scheduled jobs: cancel/reschedule actions for anything queued to fire later.

The list view has moved to the Campaigns page's "Scheduled" status filter;
this module now only redirects there and serves the cancel/reschedule API.
"""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

from apps.accounts.utils import get_current_account
from apps.scheduler.api import SchedulingError, cancel_job, reschedule_job
from apps.scheduler.models import ScheduledJob


@login_required
def scheduled_index(request):
    return redirect("/email/campaigns/?status=scheduled")


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

"""Account analytics pages (apps.publishing.account_metrics). Behind the
``cs_analytics`` release gate: until it is released, ReleaseGateMiddleware 404s them."""

from __future__ import annotations

import csv
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.api import is_account_admin
from apps.accounts.utils import get_current_account
from apps.conversations import reporting
from apps.core.module_gate import module_required

from . import account_metrics
from .models import PublishingSettings
from .views import _in_workspace_tz


def _days(request) -> int:
    try:
        days = int(request.GET.get("period", 30))
    except (TypeError, ValueError):
        days = 30
    return days if days in reporting.PERIODS else 30


def _context(account, days: int) -> dict:
    from .results import journey, publishing_channels

    channels = [
        p for p in account_metrics.PLATFORMS if p in publishing_channels(account)
    ]
    summaries = [account_metrics.summary(account, p, days) for p in channels]
    end = account_metrics.settled_day(account)
    period = reporting.last_days(days)
    return {
        "summaries": summaries,
        "days": days,
        "periods": reporting.PERIODS,
        "end": end,
        "start": end - datetime.timedelta(days=days - 1),
        "journey": journey(account, period),
        "settle_days": account_metrics.SETTLE_DAYS,
    }


@login_required
@module_required("content_studio")
@_in_workspace_tz
def analytics(request):
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    settings_row = PublishingSettings.objects.filter(account=account).first()
    return render(
        request,
        "publishing/analytics.html",
        {
            "account": account,
            **_context(account, _days(request)),
            "weekly_report": bool(settings_row and settings_row.weekly_report),
            "can_admin": is_account_admin(request.user, account),
        },
    )


@login_required
@module_required("content_studio")
@_in_workspace_tz
def analytics_print(request):
    """The same numbers as a page made for printing or "Save as PDF"."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    return render(
        request,
        "publishing/analytics_print.html",
        {
            "account": account,
            **_context(account, _days(request)),
            "now": timezone.now(),
        },
    )


@login_required
@module_required("content_studio")
@_in_workspace_tz
def analytics_csv(request):
    """Every channel's daily numbers. A number that wasn't read is blank, with its
    state beside it, never 0."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    days = _days(request)
    end = account_metrics.settled_day(account)
    start = end - datetime.timedelta(days=days - 1)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="akilent-analytics-{start}-to-{end}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(["day", "channel", "metric", "value", "state"])
    for row in account_metrics.daily_rows(account, start, end):
        writer.writerow(
            [row["day"], row["channel"], row["metric"], row["value"], row["state"]]
        )
    return response


@login_required
@module_required("content_studio")
@require_POST
def analytics_weekly(request):
    """Turn the weekly email to Owners and Admins on or off."""
    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    if not is_account_admin(request.user, account):
        messages.error(request, "Only an Owner or Admin can change this.")
        return redirect("publishing:analytics")
    on = request.POST.get("weekly_report") == "on"
    PublishingSettings.objects.update_or_create(
        account=account, defaults={"weekly_report": on}
    )
    messages.success(
        request,
        "Weekly email turned on." if on else "Weekly email turned off.",
    )
    return redirect("publishing:analytics")

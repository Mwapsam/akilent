"""Business Health: what Akilent did for this business. See ``apps.conversations.reporting``.

Rendered in one request, like the page it replaces (``apps.email.views.insights``) — Insights is
read by opening it, not by polling, so there's no lazy HTMX split here the way the dashboard has
one for its background aggregates.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts.utils import get_current_account
from apps.conversations import reporting

DEFAULT_PERIOD = 30


def _period(request, account, now) -> tuple[reporting.Period, int, bool]:
    """The chosen period, clamped to what the plan allows. ``(period, days, period_locked)``."""
    from apps.billing.limits import LimitChecker

    try:
        days = int(request.GET.get("period", DEFAULT_PERIOD))
    except (TypeError, ValueError):
        days = DEFAULT_PERIOD
    if days not in reporting.PERIODS:
        days = DEFAULT_PERIOD
    has_history = LimitChecker(account).has_feature("detailed_analytics")
    locked = days == 90 and not has_history
    if locked:
        days = DEFAULT_PERIOD
    return reporting.last_days(days, now), days, locked


@login_required
def insights(request):
    from apps.billing.limits import LimitChecker
    from apps.conversations import api as conversations_api
    from apps.email.views import email_analytics_context

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    now = timezone.now()
    period, days, period_locked = _period(request, account, now)
    has_history = LimitChecker(account).has_feature("detailed_analytics")
    rows = reporting.first_replies(account, period.start, period.end)
    peak_hours = reporting.peak_hours(account, period, rows)

    from apps.conversations.performance import followup_completion

    # Shaped for the chart here, not in the template, for the same reason the email engagement
    # chart is: a template can't check a chart drawn from mismatched columns, and that's wrong
    # without looking wrong.
    from apps.conversations.templatetags.insights_tags import hour12

    peak_labels = [hour12(h) for h in range(24)]
    peak_series = [("Enquiries", peak_hours.get("hours", [0] * 24))]

    return render(
        request,
        "insights/index.html",
        {
            "account": account,
            "days": days,
            "period_choices": reporting.PERIODS,
            "period_locked": period_locked,
            "has_history": has_history,
            "proof": reporting.proof(account, now=now),
            "at_risk": reporting.at_risk(account, now),
            "starting_point": conversations_api.starting_point(account, now),
            "response": reporting.response(account, period, rows),
            "recovery": reporting.recovery(account, period),
            "followups": followup_completion(account, days=days, now=now),
            "peak_hours": peak_hours,
            "peak_labels": peak_labels,
            "peak_series": peak_series,
            "team": reporting.team(account, period, rows),
            "automation": reporting.automation_impact(account, period, rows),
            "sales": reporting.sales(account, period),
            "channels": reporting.channels(account, period, rows)
            if has_history
            else [],
            **email_analytics_context(account, request),
        },
    )

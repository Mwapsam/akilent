"""Business Health: what Akilent did for this business. See ``apps.conversations.reporting``.

Rendered in one request, like the page it replaces (``apps.email.views.insights``) — Insights is
read by opening it, not by polling, so there's no lazy HTMX split here the way the dashboard has
one for its background aggregates.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts.utils import get_current_account
from apps.conversations import reporting
from apps.conversations.models import InsightGoal, InsightSettings

DEFAULT_PERIOD = 30
FREE_GOAL_LIMIT = 3  # every plan; plans with detailed_analytics get unlimited goals


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
    from apps.accounts.api import is_account_admin
    from apps.billing.limits import LimitChecker
    from apps.conversations import api as conversations_api
    from apps.email.views import email_analytics_context

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")

    now = timezone.now()
    period, days, period_locked = _period(request, account, now)
    has_history = LimitChecker(account).has_feature("detailed_analytics")
    goals = reporting.goal_progress(account, now=now)
    goal_limit = None if has_history else FREE_GOAL_LIMIT
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
            "momentum": reporting.momentum(account),
            "goals": goals,
            "goal_limit": goal_limit,
            "goal_limit_reached": goal_limit is not None
            and goals["total"] >= goal_limit,
            "can_manage_goals": is_account_admin(request.user, account),
            "goal_metrics": reporting.GOAL_METRICS,
            "weekly_report_enabled": getattr(
                InsightSettings.objects.filter(account=account).first(),
                "weekly_report",
                True,
            ),
            # Revenue can have one goal per currency, so it's never excluded here; every other
            # metric is single-goal, so once it's set it drops off the "add a goal" choices.
            "existing_goal_metrics": {
                g.metric
                for g in InsightGoal.objects.filter(account=account)
                if g.metric != InsightGoal.Metric.REVENUE
            },
            **email_analytics_context(account, request),
        },
    )


def _goals_redirect(request):
    query = request.GET.urlencode()
    return redirect(f"/insights/?{query}" if query else "/insights/")


def _require_goal_permission(request, account) -> bool:
    """True if allowed; otherwise a message is queued and the caller should redirect. Shared by
    goal changes and the weekly report toggle — both are owner/admin-only settings."""
    from apps.accounts.api import is_account_admin

    if not is_account_admin(request.user, account):
        messages.error(request, "Only an owner or admin can change this.")
        return False
    return True


@login_required
def insights_goal_create(request):
    from apps.billing.limits import LimitChecker

    account = get_current_account(request)
    if account is None or request.method != "POST":
        return redirect("dashboard")
    if not _require_goal_permission(request, account):
        return _goals_redirect(request)

    has_history = LimitChecker(account).has_feature("detailed_analytics")
    existing = InsightGoal.objects.filter(account=account).count()
    if not has_history and existing >= FREE_GOAL_LIMIT:
        messages.error(
            request,
            f"Your plan includes up to {FREE_GOAL_LIMIT} goals. Upgrade for unlimited goals.",
        )
        return _goals_redirect(request)

    metric = request.POST.get("metric", "")
    currency = (request.POST.get("currency") or "").strip().upper()
    info = reporting.GOAL_METRICS.get(metric)
    if info is None:
        messages.error(request, "Choose a goal to track.")
        return _goals_redirect(request)
    if info["needs_currency"] and not currency:
        messages.error(request, "Choose a currency for this goal.")
        return _goals_redirect(request)
    if not info["needs_currency"]:
        currency = ""
    try:
        target = Decimal(request.POST.get("target", "").strip())
        if target <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        messages.error(request, "Enter a target greater than zero.")
        return _goals_redirect(request)

    try:
        InsightGoal.objects.create(
            account=account,
            metric=metric,
            target=target,
            currency=currency,
            created_by=request.user,
        )
    except IntegrityError:
        messages.error(request, "You already have a goal for that.")
        return _goals_redirect(request)
    messages.success(request, "Goal added.")
    return _goals_redirect(request)


@login_required
def insights_goal_update(request, pk):
    account = get_current_account(request)
    if account is None or request.method != "POST":
        return redirect("dashboard")
    if not _require_goal_permission(request, account):
        return _goals_redirect(request)

    goal = InsightGoal.objects.filter(account=account, pk=pk).first()
    if goal is None:
        messages.error(request, "That goal doesn't exist any more.")
        return _goals_redirect(request)
    try:
        target = Decimal(request.POST.get("target", "").strip())
        if target <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        messages.error(request, "Enter a target greater than zero.")
        return _goals_redirect(request)
    goal.target = target
    goal.save(update_fields=["target", "updated_at"])
    messages.success(request, "Goal updated.")
    return _goals_redirect(request)


@login_required
def insights_goal_delete(request, pk):
    account = get_current_account(request)
    if account is None or request.method != "POST":
        return redirect("dashboard")
    if not _require_goal_permission(request, account):
        return _goals_redirect(request)

    InsightGoal.objects.filter(account=account, pk=pk).delete()
    messages.success(request, "Goal removed.")
    return _goals_redirect(request)


@login_required
def insights_report_settings(request):
    """Turn the weekly owner report on or off (see ``apps.conversations.weekly_report``)."""
    account = get_current_account(request)
    if account is None or request.method != "POST":
        return redirect("dashboard")
    if not _require_goal_permission(request, account):
        return _goals_redirect(request)

    enabled = request.POST.get("weekly_report") == "on"
    InsightSettings.objects.update_or_create(
        account=account, defaults={"weekly_report": enabled}
    )
    messages.success(
        request, "Weekly report turned on." if enabled else "Weekly report turned off."
    )
    return _goals_redirect(request)

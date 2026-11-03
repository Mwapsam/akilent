"""The "Which posts sold" report (apps.publishing.sales). Behind the ``cs_sales_report``
release gate: until it is released, ReleaseGateMiddleware 404s this page."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.accounts.utils import get_current_account
from apps.conversations import reporting
from apps.core.module_gate import module_required

from . import sales


@login_required
@module_required("content_studio")
def sales_report(request):
    # The same periods, and the same plan rule for 90 days, as Insights.
    from apps.conversations.insights_views import _period

    account = get_current_account(request)
    if account is None:
        return redirect("dashboard")
    period, days, period_locked = _period(request, account, timezone.now())
    return render(
        request,
        "publishing/sales.html",
        {
            "account": account,
            "report": sales.report(account, period),
            "days": days,
            "periods": reporting.PERIODS,
            "period_locked": period_locked,
        },
    )

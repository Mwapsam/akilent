"""Unit economics: what a plan can cost Akilent at most, and what each business actually cost.

Only costs Akilent pays per unit count: email (provider) and AI (model). WhatsApp messages are
billed by Meta to each business directly and Akilent must not resell or mark them up, so they are
never part of a plan's cost. Hosting, support and the like are one fixed monthly cost per business.

Reached through ``apps.billing.api``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone

from apps.billing.models import (
    CostSettings,
    Plan,
    PlanLimit,
    Subscription,
    UnitCost,
    UsageCounter,
)

DAYS_PER_MONTH = 30


@dataclass(frozen=True)
class Driver:
    key: str
    name: str
    unit: str
    month_limit: str = ""  # a monthly limit that caps this driver
    day_limit: str = ""  # a daily limit that caps it (x30 for the month)
    site_day_setting: str = (
        ""  # a site-wide daily ceiling per business that applies above every plan
    )


DRIVERS = (
    Driver(
        "email", "Email", "email", month_limit="emails_month", day_limit="emails_day"
    ),
    Driver(
        "ai_action",
        "AI action",
        "action",
        day_limit="ai_actions_day",
        site_day_setting="AI_DAILY_CALL_LIMIT",
    ),
)
BY_KEY = {d.key: d for d in DRIVERS}


def unit_costs() -> dict[str, Decimal]:
    rows = dict(UnitCost.objects.values_list("driver", "cost_per_unit"))
    return {d.key: rows.get(d.key, Decimal("0")) for d in DRIVERS}


def _plan_limits(plan: Plan, overrides: dict | None = None) -> dict:
    values = dict(PlanLimit.objects.filter(plan=plan).values_list("key", "value"))
    values.update(overrides or {})
    return values


def max_units(driver: Driver, limits: dict) -> int | None:
    """The most units a business on these limits can use in a month. None = uncapped."""
    caps = []
    if driver.month_limit and limits.get(driver.month_limit, -1) >= 0:
        caps.append(limits[driver.month_limit])
    if driver.day_limit and limits.get(driver.day_limit, -1) >= 0:
        caps.append(limits[driver.day_limit] * DAYS_PER_MONTH)
    if driver.site_day_setting:
        from django.conf import settings

        ceiling = int(getattr(settings, driver.site_day_setting, 0) or 0)
        if ceiling > 0:
            caps.append(ceiling * DAYS_PER_MONTH)
    return min(caps) if caps else None


def plan_economics(
    plan: Plan,
    *,
    limit_overrides: dict | None = None,
    price: Decimal | None = None,
    is_active: bool | None = None,
) -> dict:
    """Worst-case monthly cost of one business on ``plan`` (every cost-bearing limit fully used),
    the margin at its price, and why it could lose money.

    ``price``/``is_active`` let a caller ask "what if" without writing to ``plan`` or
    constructing a stand-in instance."""
    price = plan.price_monthly if price is None else price
    is_active = plan.is_active if is_active is None else is_active
    settings = CostSettings.load()
    costs = unit_costs()
    limits = _plan_limits(plan, limit_overrides)
    from typing import Any

    lines: list[dict[str, Any]] = []
    uncapped: list[str] = []
    total = settings.fixed_monthly_cost_per_business
    for d in DRIVERS:
        units = max_units(d, limits)
        if units is None:
            if costs[d.key] > 0:
                uncapped.append(d.name)
            lines.append(
                {"driver": d, "units": None, "cost": None, "unit_cost": costs[d.key]}
            )
            continue
        cost = (costs[d.key] * units).quantize(Decimal("0.01"))
        total += cost
        lines.append(
            {"driver": d, "units": units, "cost": cost, "unit_cost": costs[d.key]}
        )
    margin = price - total
    margin_pct = round(margin * 100 / price) if price > 0 else None
    reasons = []
    if price > 0 and uncapped:
        reasons.append(
            f"{', '.join(uncapped)} {'is' if len(uncapped) == 1 else 'are'} unlimited, so the cost has no ceiling."
        )
    if price > 0 and total > price:
        reasons.append(
            f"At full use it costs ${total:.2f} a month, more than its ${price:.2f} price."
        )
    return {
        "plan": plan,
        "lines": lines,
        "fixed": settings.fixed_monthly_cost_per_business,
        "worst_cost": total,
        "price": price,
        "margin": margin,
        "margin_pct": margin_pct,
        "below_target": price > 0
        and (
            bool(uncapped)
            or (margin_pct is not None and margin_pct < settings.target_margin_pct)
        ),
        "can_lose_money": bool(reasons),
        "reasons": reasons,
        "uncapped": uncapped,
    }


def loss_reasons(
    plan: Plan, *, limit_overrides: dict | None = None, price=None, is_active=None
) -> list[str]:
    """Why saving ``plan`` (with these changes) could sell it at a loss. Only a paid, listed plan
    is checked, and only cost-bearing drivers count: an unlimited WhatsApp or conversation limit
    never makes a plan unsafe."""
    effective_price = plan.price_monthly if price is None else price
    effective_active = plan.is_active if is_active is None else is_active
    if not effective_active or effective_price <= 0:
        return []
    return plan_economics(
        plan,
        limit_overrides=limit_overrides,
        price=effective_price,
        is_active=effective_active,
    )["reasons"]


def _meter_key(driver: Driver) -> str:
    """The UsageCounter key that meters this driver: its monthly limit if it has one, else its
    daily limit (summed over the month, since counters are per day-period)."""
    return driver.month_limit or driver.day_limit


def _month_usage(driver: Driver, *, account=None, month_start):  # type: ignore[return]
    qs = UsageCounter.objects.filter(key=_meter_key(driver))
    qs = qs.filter(account=account) if account is not None else qs
    qs = (
        qs.filter(period_start=month_start)
        if driver.month_limit
        else qs.filter(period_start__gte=month_start)
    )
    return qs


def actual_cost(account, *, now=None) -> dict:
    """What this business cost Akilent this calendar month so far, against what its plan charges."""
    now = now or timezone.now()
    month_start = now.date().replace(day=1)
    used = {
        d.key: sum(
            _month_usage(d, account=account, month_start=month_start).values_list(
                "used", flat=True
            )
        )
        for d in DRIVERS
    }
    sub = Subscription.objects.filter(account=account).select_related("plan").first()
    return _cost_of(
        used, unit_costs(), CostSettings.load().fixed_monthly_cost_per_business, sub
    )


def _cost_of(used: dict, costs: dict, fixed, sub) -> dict:
    lines = [
        {
            "driver": BY_KEY[k],
            "units": n,
            "cost": (costs[k] * n).quantize(Decimal("0.01")),
        }
        for k, n in used.items()
    ]
    total = fixed + sum(line["cost"] for line in lines)
    price = sub.plan.price_monthly if sub else Decimal("0")
    return {
        "lines": lines,
        "fixed": fixed,
        "total": total,
        "price": price,
        "margin": price - total,
        "plan": sub.plan if sub else None,
    }


def businesses_by_margin(limit: int = 25) -> list[dict]:
    """Businesses with a subscription, the least profitable first (loss-making at the top)."""
    from django.db.models import Sum

    from apps.accounts.models import Account

    month_start = timezone.now().date().replace(day=1)
    costs = unit_costs()
    fixed = CostSettings.load().fixed_monthly_cost_per_business

    def totals(driver):  # one grouped query for every business
        found = (
            _month_usage(driver, month_start=month_start)
            .values("account_id")
            .annotate(n=Sum("used"))
        )
        return {r["account_id"]: r["n"] or 0 for r in found}

    by_driver = {d.key: totals(d) for d in DRIVERS}
    rows = []
    for account in Account.objects.filter(subscription__isnull=False).select_related(
        "subscription__plan"
    ):
        used = {
            k: totals_by_account.get(account.pk, 0)
            for k, totals_by_account in by_driver.items()
        }
        rows.append(
            dict(_cost_of(used, costs, fixed, account.subscription), account=account)
        )
    rows.sort(key=lambda r: r["margin"])
    return rows[:limit]

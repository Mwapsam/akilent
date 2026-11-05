"""Which posts sold (Phase 1, ``cs_sales_report``).

One row per post: the chats it started, the leads and orders credited to it, and its
paid revenue, per currency and never summed across currencies. Same evidence as the
results card (``apps.publishing.results``): only chats that named a post count, so
these are lower bounds.

Each credit keeps how sure it is (``bio.credit_basis``, read from the chat's entry
point). Only confirmed credits rank a post. "Via bio link, post not confirmed" and
"direct link" credits are shown beside it but never move it up. A post with fewer than
``MIN_CHATS`` confirmed chats in the period is listed as too early to judge, not ranked.

What no post is credited with (the recent-chat guess, and everything else) is shown
separately, from the same period, so the credited share isn't read as the whole.

Periods follow the journey: chats by when they were matched, leads and orders by when
they were created, revenue by when it was paid.

The tiles at the top (``kpis``) total the confirmed credits, with a trend line across the
period and the change against the period before it. The same queries read both periods (the
date range is twice as long), so the tiles cost nothing extra.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from . import bio
from .models import BioPage, ContentPost
from .results import journey, post_credit_q

FLAG = "cs_sales_report"
MIN_CHATS = 5  # confirmed chats a post needs in the period before it is ranked
UNCONFIRMED = (bio.BIO_UNCONFIRMED, bio.DIRECT_LINK)


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def _bucket() -> dict:
    return {
        "chats": 0,
        "leads": 0,
        "orders": 0,
        "paid_orders": 0,
        "revenue": defaultdict(Decimal),
    }


def _basis(entry, page) -> str:
    # A credit with no entry point was named by a person or workflow: confirmed.
    return bio.credit_basis(entry, page) if entry is not None else bio.CONFIRMED


def _revenue_list(totals: dict) -> list[dict]:
    return [{"currency": c, "total": totals[c]} for c in sorted(totals)]


def _finish(bucket: dict) -> dict:
    return {**bucket, "revenue": _revenue_list(bucket["revenue"])}


SPARK_POINTS = 12  # at most; a 7-day period has one point a day
KPI_LABELS = (
    ("chats", "Chats"),
    ("leads", "Leads"),
    ("orders", "Orders"),
    ("paid_orders", "Paid orders"),
)


def _change(now, before) -> dict | None:
    """The change against the period before: ``{"pct", "up", "flat"}``, ``{"new": True}``
    when there was nothing before, or None when there's nothing either side (no made-up 0%)."""
    if not before:
        return {"new": True, "up": True} if now else None
    pct = round((now - before) / before * 100)
    return {"pct": abs(pct), "up": pct >= 0, "flat": pct == 0}


class _Trend:
    """Counts per slot across the period, for a sparkline (viewBox 0 0 100 28)."""

    def __init__(self, period):
        self.start = period.start
        self.n = max(2, min(SPARK_POINTS, period.days))
        self.width = (period.end - period.start) / self.n
        self.values: dict[str, list[int]] = defaultdict(lambda: [0] * self.n)

    def add(self, metric: str, when: datetime) -> None:
        i = min(self.n - 1, max(0, int((when - self.start) / self.width)))
        self.values[metric][i] += 1

    def spark(self, metric: str, tz) -> dict:
        from django.utils import timezone as djtz

        values = self.values[metric]
        top = max(values) or 1
        pts = [
            (round(i * 100 / (self.n - 1), 2), round(26 - v / top * 24, 2))
            for i, v in enumerate(values)
        ]
        slots = []
        for i, v in enumerate(values):
            day = djtz.localtime(self.start + self.width * i, tz)
            slots.append(
                {
                    "x": round(i * 100 / self.n, 2),
                    "w": round(100 / self.n, 2),
                    "label": f"{day.day} {day:%b}",
                    "value": v,
                }
            )
        return {
            "points": " ".join(f"{x},{y}" for x, y in pts),
            "end": pts[-1],
            "slots": slots,
        }


def _funnel(counts: dict) -> list[dict]:
    """Chats → leads → orders → paid for one post, each as a share of the widest step (a lead
    can be credited without a chat in the period, so the widest isn't always chats)."""
    top = max((counts[m] for m, _ in KPI_LABELS), default=0) or 1
    return [
        {"label": label, "value": counts[m], "pct": round(counts[m] / top * 100)}
        for m, label in KPI_LABELS
    ]


def _zone(name: str):
    from zoneinfo import ZoneInfo

    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return ZoneInfo("UTC")


def report(account, period, min_chats: int = MIN_CHATS) -> dict | None:
    """``{ranked, too_early, credited, unconfirmed, not_credited, min_chats}``, or None
    when the business can't publish posts at all."""
    from apps.commerce.models import Order
    from apps.conversations.models import ConversationEntryPoint
    from apps.crm.models import Lead

    overall = journey(account, period)
    if overall is None:
        return None
    page = BioPage.objects.filter(account=account).first()
    # Both periods in one read: the one before only feeds the tiles' comparison.
    during = {"__gte": period.previous().start, "__lt": period.end}
    trend = _Trend(period)
    earlier = _bucket()  # confirmed credits in the period before

    def in_period(field):
        return {field + k: v for k, v in during.items()}

    # post id -> basis -> counts
    posts: dict[int, dict[str, dict]] = defaultdict(lambda: defaultdict(_bucket))
    entry_related = ("variant", "link__variant")

    def credit(post_id, basis, metric, when, revenue=None, currency=""):
        """One credit: to its post in this period (and the trend), or to the period before."""
        if when < period.start:
            if basis == bio.CONFIRMED:
                earlier[metric] += 1
                if revenue is not None:
                    earlier["revenue"][currency] += revenue
            return
        b = posts[post_id][basis]
        b[metric] += 1
        if revenue is not None:
            b["revenue"][currency] += revenue
        if basis == bio.CONFIRMED:
            trend.add(metric, when)

    seen = set()
    for entry in (
        ConversationEntryPoint.objects.filter(
            account=account, post__isnull=False, **in_period("matched_at")
        )
        .select_related(*entry_related)
        .order_by("matched_at", "pk")
    ):
        key = (entry.matched_at >= period.start, entry.post_id, entry.conversation_id)
        if key in seen:
            continue  # the chat's first entry from this post decides its basis
        seen.add(key)
        credit(entry.post_id, _basis(entry, page), "chats", entry.matched_at)

    # The filters below join through source_post, so source_post_id is never None.
    attribution_related = [f"attribution__entry_point__{r}" for r in entry_related]
    credited = post_credit_q()
    for lead in Lead.objects.filter(
        credited,
        account=account,
        attribution__source_post__account=account,
        **in_period("created_at"),
    ).select_related("attribution__entry_point", *attribution_related):
        a = lead.attribution
        basis = _basis(a.entry_point, page)
        credit(cast("int", a.source_post_id), basis, "leads", lead.created_at)

    orders = Order.objects.filter(
        credited, account=account, attribution__source_post__account=account
    ).select_related("attribution__entry_point", *attribution_related)
    for order in orders.filter(**in_period("created_at")):
        a = order.attribution
        basis = _basis(a.entry_point, page)
        credit(cast("int", a.source_post_id), basis, "orders", order.created_at)
    for order in orders.filter(status=Order.Status.PAID, **in_period("paid_at")):
        a = order.attribution
        credit(
            cast("int", a.source_post_id),
            _basis(a.entry_point, page),
            "paid_orders",
            order.paid_at,
            revenue=order.total or Decimal("0"),
            currency=order.currency,
        )

    by_pk = ContentPost.objects.in_bulk(list(posts))
    rows: list[dict[str, Any]] = []
    totals: dict[str, dict[str, Decimal]] = {
        basis: defaultdict(Decimal) for basis in (bio.CONFIRMED, *UNCONFIRMED)
    }
    for pk, buckets in posts.items():
        post = by_pk.get(pk)
        if post is None:
            continue
        for basis, b in buckets.items():
            for currency, amount in b["revenue"].items():
                totals[basis][currency] += amount
        unconfirmed = {
            basis: _finish(buckets[basis]) for basis in UNCONFIRMED if basis in buckets
        }
        confirmed = _finish(buckets[bio.CONFIRMED])
        rows.append(
            {
                "post": post,
                "confirmed": confirmed,
                "funnel": _funnel(confirmed),
                "unconfirmed": [
                    {"label": bio.LABELS[basis], "basis": basis, **counts}
                    for basis, counts in unconfirmed.items()
                ],
            }
        )

    def rank_key(row):
        c = row["confirmed"]
        return (
            -c["paid_orders"],
            -c["orders"],
            -c["leads"],
            -c["chats"],
            -row["post"].pk,
        )

    ranked = sorted(
        (r for r in rows if r["confirmed"]["chats"] >= min_chats), key=rank_key
    )
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
    too_early = sorted(
        (r for r in rows if r["confirmed"]["chats"] < min_chats), key=rank_key
    )
    from apps.accounts.business_hours import workspace_timezone

    tz = _zone(workspace_timezone(account))
    now = {m: sum(r["confirmed"][m] for r in rows) for m, _ in KPI_LABELS}
    kpis = [
        {
            "key": metric,
            "label": label,
            "value": now[metric],
            "change": _change(now[metric], earlier[metric]),
            "trend": trend.spark(metric, tz),
        }
        for metric, label in KPI_LABELS
    ]
    revenue = [
        {**r, "change": _change(r["total"], earlier["revenue"].get(r["currency"], 0))}
        for r in _revenue_list(totals[bio.CONFIRMED])
    ]
    return {
        "kpis": kpis,
        "revenue": revenue,
        "days": period.days,
        "ranked": ranked,
        "too_early": too_early,
        "min_chats": min_chats,
        "credited": _revenue_list(totals[bio.CONFIRMED]),
        "unconfirmed": [
            {"label": bio.LABELS[basis], "revenue": _revenue_list(totals[basis])}
            for basis in UNCONFIRMED
            if totals[basis]
        ],
        "not_credited": {
            "chats": overall["untracked_chats"],
            "guessed_revenue": overall["guessed_revenue"],
            "untracked_revenue": overall["untracked_revenue"],
        },
    }

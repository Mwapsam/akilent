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
"""

from __future__ import annotations

from collections import defaultdict
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
    during = {"__gte": period.start, "__lt": period.end}

    def in_period(field):
        return {field + k: v for k, v in during.items()}

    # post id -> basis -> counts
    posts: dict[int, dict[str, dict]] = defaultdict(lambda: defaultdict(_bucket))
    entry_related = ("variant", "link__variant")

    seen = set()
    for entry in (
        ConversationEntryPoint.objects.filter(
            account=account, post__isnull=False, **in_period("matched_at")
        )
        .select_related(*entry_related)
        .order_by("matched_at", "pk")
    ):
        key = (entry.post_id, entry.conversation_id)
        if key in seen:
            continue  # the chat's first entry from this post decides its basis
        seen.add(key)
        posts[entry.post_id][_basis(entry, page)]["chats"] += 1

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
        posts[cast("int", a.source_post_id)][_basis(a.entry_point, page)]["leads"] += 1

    orders = Order.objects.filter(
        credited, account=account, attribution__source_post__account=account
    ).select_related("attribution__entry_point", *attribution_related)
    for order in orders.filter(**in_period("created_at")):
        a = order.attribution
        posts[cast("int", a.source_post_id)][_basis(a.entry_point, page)]["orders"] += 1
    for order in orders.filter(status=Order.Status.PAID, **in_period("paid_at")):
        a = order.attribution
        b = posts[cast("int", a.source_post_id)][_basis(a.entry_point, page)]
        b["paid_orders"] += 1
        b["revenue"][order.currency] += order.total or Decimal("0")

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
        rows.append(
            {
                "post": post,
                "confirmed": _finish(buckets[bio.CONFIRMED]),
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
    return {
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

"""Posts worth promoting: what already sold, before any ad money is spent (Gate 2 of
docs/strategy/akilent-ads.md, release flag ``cs_promote_candidates``).

Evidence is the "Which posts sold" report's confirmed organic credit (``sales.report``):
explicit attribution only, and never a chat that came from a Meta ad (paid takes
precedence, see apps.conversations.ad_referrals). Rule-based; AI plays no part.

Each post gets the strongest label whose floor it clears:

- "Already sold": at least ``FLOORS["sold"]`` paid orders
- "Most leads":   at least ``FLOORS["leads"]`` leads
- "Most chats":   at least ``FLOORS["chats"]`` chats

A single order never earns "Already sold". Below every floor, no post is recommended and
the page says there isn't enough evidence yet. Floors are proposals until the Gate 0 data
check (``manage.py ads_evidence_report``) settles them.

Limits shown with every candidate: past sales don't prove a product is still available
(Akilent has no post-to-product link, so availability is "not verified"), and doing well
organically doesn't guarantee doing well as an ad.
"""

from __future__ import annotations

from datetime import datetime

from django.utils import timezone

FLAG = "cs_promote_candidates"
DAYS = 90
FLOORS = {"sold": 3, "leads": 3, "chats": 3}
LABELS = {
    "sold": "Already sold",
    "leads": "Most leads",
    "chats": "Most chats",
}
# The count behind each label, for sorting and the one-line evidence.
_METRIC = {"sold": "paid_orders", "leads": "leads", "chats": "chats"}
_STRENGTH = {"sold": 0, "leads": 1, "chats": 2}
LIMIT = 5


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def label_for(counts: dict) -> str | None:
    """The strongest label whose floor these confirmed counts clear, or None."""
    if counts.get("paid_orders", 0) >= FLOORS["sold"]:
        return "sold"
    if counts.get("leads", 0) >= FLOORS["leads"]:
        return "leads"
    if counts.get("chats", 0) >= FLOORS["chats"]:
        return "chats"
    return None


def _age_days(post, now: datetime) -> int | None:
    published = post.published_at
    return (now - published).days if published else None


def _boost_links(post) -> list[dict]:
    """Where the owner boosts the post: Meta's own post pages. Akilent spends nothing."""
    from .models import ContentPostVariant

    links = []
    for variant in post.variants.filter(
        status=ContentPostVariant.Status.PUBLISHED
    ).exclude(publication_url=""):
        links.append(
            {"platform": variant.get_platform_display(), "url": variant.publication_url}
        )
    return links


def candidates(account, *, now: datetime | None = None) -> dict | None:
    """``{days, floors, candidates: [...], enough}`` or None when the business can't publish.

    Each candidate: ``post, label, label_text, chats, leads, paid_orders, revenue,
    age_days, boost_links``. ``enough`` is False when no post clears any floor.
    """
    rows = candidates_all(account, now=now)
    if rows is None:
        return None
    out = [c for c in rows.values() if c["label"]]
    out.sort(
        key=lambda c: (
            _STRENGTH[c["label"]],
            -c[_METRIC[c["label"]]],
            -c["paid_orders"],
            -c["leads"],
            -c["chats"],
            -c["post"].pk,
        )
    )
    return {
        "days": DAYS,
        "floors": FLOORS,
        "labels": LABELS,
        "candidates": out[:LIMIT],
        "enough": bool(out),
    }


def for_post(post, *, now: datetime | None = None) -> dict | None:
    """This post's own evidence: its label (or None below every floor) and counts.

    None when the business can't publish. Same evidence and floors as ``candidates``.
    """
    data = candidates_all(post.account, now=now)
    if data is None:
        return None
    return data.get(post.pk) or {
        "post": post,
        "label": None,
        "label_text": "",
        "chats": 0,
        "leads": 0,
        "paid_orders": 0,
        "revenue": [],
        "age_days": _age_days(post, now or timezone.now()),
        "boost_links": [],
    }


def candidates_all(account, *, now: datetime | None = None) -> dict | None:
    """Every post with confirmed organic credit in the window, by post pk (labelled or not)."""
    from apps.conversations import reporting

    from . import sales

    now = now or timezone.now()
    report = sales.report(account, reporting.last_days(DAYS, now), min_chats=0)
    if report is None:
        return None
    by_pk = {}
    for row in report["ranked"]:
        counts = row["confirmed"]
        label = label_for(counts)
        post = row["post"]
        by_pk[post.pk] = {
            "post": post,
            "label": label,
            "label_text": LABELS.get(label or "", ""),
            "chats": counts["chats"],
            "leads": counts["leads"],
            "paid_orders": counts["paid_orders"],
            "revenue": counts["revenue"],
            "age_days": _age_days(post, now),
            "boost_links": _boost_links(post) if label else [],
        }
    return by_pk

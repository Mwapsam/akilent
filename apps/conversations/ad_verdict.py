"""What happened to the chats each Meta ad brought in (Gate 3 of
docs/strategy/akilent-ads.md, release flag ``ads_inbox_verdict``).

One row per ad (``AdReferral.source_id``) whose chats started in the period:

- **chats**: conversations that came from the ad.
- **First reply**: the clock starts at the first customer message of the ad conversation
  (``AdReferral.message``; the referral time for Instagram's standalone event) and stops at
  the business's first *qualifying* reply. Later customer messages don't restart it.
- **Qualifying reply**: any reply actually sent (``state.RESPONSE_STATUSES``), by a person or
  an automation that answers, except a generic acknowledgement: the system's own messages
  (``sent_by == "system"``, e.g. the setup confirmation) and AI's holding reply
  (``auto_kind == "holding"``). Human or automated is recorded from ``sent_by``.
- **Waited too long**: no qualifying reply within ``UNANSWERED_SECONDS`` of *business-hours*
  time (``business_hours.open_seconds_between``), including chats never answered once that
  much open time has passed. Total elapsed time is reported beside it.
- **Follow-ups**, **leads** and **paid orders** (per currency, never summed across
  currencies) from explicit attribution through the ad's entry point.

Every number is Akilent-attributed. Spend and other Meta-reported figures need ``ads_read``
(Gate 4) and are not here. Counts, never causes: the verdict says what happened, not why.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median

from django.db.models import Q
from django.utils import timezone

FLAG = "ads_inbox_verdict"
UNANSWERED_SECONDS = 60 * 60  # §10f proposal: 1 business hour
ROW_LIMIT = 2000  # referrals per period
AUTOMATED = ("ai", "automation")


def is_released() -> bool:
    from apps.core.releases import is_released as released

    return released(FLAG)


def _sent_replies():
    from apps.conversations.models import Message
    from apps.conversations.state import RESPONSE_STATUSES

    return Message.objects.filter(
        direction=Message.Direction.OUTBOUND, status__in=RESPONSE_STATUSES
    )


def is_generic(metadata: dict | None) -> bool:
    """A system message or AI's holding reply: an acknowledgement, not an answer.

    Checked in Python on purpose: excluding on a JSON key in SQL also drops every message
    that lacks the key (NULL comparisons), which is every reply a person sent."""
    meta = metadata or {}
    return meta.get("sent_by") == "system" or meta.get("auto_kind") == "holding"


def _row(ref) -> dict:
    return {
        "source_id": ref.source_id,
        "headline": ref.headline,
        "channel": ref.get_channel_display(),
        "post": ref.post,
        "source_url": ref.source_url,
        "chats": 0,
        "replied": 0,
        "replied_by_person": 0,
        "replied_automatically": 0,
        "waited_too_long": 0,
        "never_replied": 0,
        "followed_up": 0,
        "leads": 0,
        "orders": 0,
        "paid_orders": 0,
        "revenue": defaultdict(Decimal),
        "_business": [],
        "_total": [],
        "_conversations": set(),
        "_refs": [],
    }


def conversation_reply(conversation_id: int, start, hours, now) -> dict:
    """The first qualifying reply after ``start``: ``{replied, by, total, business, late}``."""
    reply = next(
        (
            m
            for m in _sent_replies()
            .filter(conversation_id=conversation_id, timestamp__gte=start)
            .order_by("timestamp", "pk")
            .values("timestamp", "metadata")[:50]
            if not is_generic(m["metadata"])
        ),
        None,
    )
    from apps.accounts.business_hours import open_seconds_between

    if reply is None:
        business = open_seconds_between(hours, start, now)
        return {
            "replied": False,
            "by": "",
            "total": None,
            "business": None,
            "late": business > UNANSWERED_SECONDS,
        }
    sent_by = (reply["metadata"] or {}).get("sent_by") or ""
    business = open_seconds_between(hours, start, reply["timestamp"])
    return {
        "replied": True,
        "by": "automation" if sent_by in AUTOMATED else "person",
        "total": (reply["timestamp"] - start).total_seconds(),
        "business": business,
        "late": business > UNANSWERED_SECONDS,
    }


def report(account, period, *, now=None) -> list[dict]:
    """One verdict row per ad with chats started in ``period``, most chats first."""
    from apps.accounts.business_hours import get_hours
    from apps.commerce.models import Order
    from apps.conversations.models import AdReferral, FollowUp
    from apps.crm.models import Lead

    now = now or timezone.now()
    hours = get_hours(account)
    refs = (
        AdReferral.objects.filter(
            account=account,
            received_at__gte=period.start,
            received_at__lt=period.end,
        )
        .select_related("message", "post")
        .order_by("received_at", "pk")[:ROW_LIMIT]
    )
    rows: dict[str, dict] = {}
    for ref in refs:
        row = rows.setdefault(ref.source_id, _row(ref))
        row["_refs"].append(ref.pk)
        if ref.conversation_id in row["_conversations"]:
            continue
        row["_conversations"].add(ref.conversation_id)
        row["chats"] += 1
        start = ref.message.timestamp if ref.message_id else ref.received_at
        r = conversation_reply(ref.conversation_id, start, hours, now)
        if r["replied"]:
            row["replied"] += 1
            row["replied_by_person" if r["by"] == "person" else "replied_automatically"] += 1
            row["_business"].append(r["business"])
            row["_total"].append(r["total"])
        else:
            row["never_replied"] += 1
        if r["late"]:
            row["waited_too_long"] += 1
        if FollowUp.objects.filter(
            conversation_id=ref.conversation_id, created_at__gte=start
        ).exists():
            row["followed_up"] += 1

    for row in rows.values():
        via_ad = Q(attribution__entry_point__ad_referral__in=row["_refs"])
        row["leads"] = Lead.objects.filter(via_ad, account=account).count()
        orders = Order.objects.filter(via_ad, account=account)
        row["orders"] = orders.count()
        for order in orders.filter(status=Order.Status.PAID):
            row["paid_orders"] += 1
            row["revenue"][order.currency] += order.total or Decimal("0")
        row["revenue"] = [
            {"currency": c, "total": row["revenue"][c]} for c in sorted(row["revenue"])
        ]
        row["median_business_seconds"] = (
            median(row["_business"]) if row["_business"] else None
        )
        row["median_total_seconds"] = median(row["_total"]) if row["_total"] else None
        for key in ("_business", "_total", "_conversations", "_refs"):
            del row[key]
    return sorted(rows.values(), key=lambda r: (-r["chats"], r["source_id"]))


def for_post(post, period, *, now=None) -> dict | None:
    """Ad chats for ads Akilent matched to this post (paid, kept apart from its organic
    numbers): ``{chats, paid_orders, revenue}``, or None when no matched ad has chats."""
    rows = [r for r in report(post.account, period, now=now) if r["post"] == post]
    if not rows:
        return None
    revenue: dict[str, Decimal] = defaultdict(Decimal)
    for r in rows:
        for item in r["revenue"]:
            revenue[item["currency"]] += item["total"]
    return {
        "ads": len(rows),
        "chats": sum(r["chats"] for r in rows),
        "waited_too_long": sum(r["waited_too_long"] for r in rows),
        "paid_orders": sum(r["paid_orders"] for r in rows),
        "revenue": [{"currency": c, "total": revenue[c]} for c in sorted(revenue)],
    }

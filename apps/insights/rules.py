"""Insight rules — pure functions that observe account data and return an Insight.

Each rule
- accepts an account
- queries data using existing platform functions where possible
- returns an Insight instance (not yet saved) or None if the condition is not met
- never has side effects

Rules are registered in RULES at the bottom.  The generate_insights command
iterates RULES and calls upsert_insight() on non-None results.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone


def rule_inactive_customers(account):
    """Customers with 2+ orders who haven't been active in 90 days."""
    from apps.contacts.models import Contact

    cutoff = timezone.now() - timedelta(days=90)

    base_qs = Contact.objects.filter(account=account)
    if hasattr(base_qs, "annotate_order_count"):
        qs = base_qs.annotate_order_count().filter(  # type: ignore[attr-defined]
            order_count__gte=2, last_engaged_at__lt=cutoff
        )
    else:
        qs = _inactive_customers_fallback(account, cutoff)

    count = qs.count()
    if count == 0:
        return None

    sample = list(qs.values_list("first_name", "last_name", "phone")[:5])
    sample_names = [f"{fn} {ln}".strip() or ph or "—" for fn, ln, ph in sample]

    from apps.insights.models import Insight

    return Insight(
        account=account,
        type="inactive_customers",
        severity=Insight.Severity.OPPORTUNITY,
        title=f"{count} customer{'s' if count != 1 else ''} may be drifting away",
        body=(
            f"{count} customers have purchased more than once but haven't been active "
            f"in the past 90 days. Re-engaging them is typically easier than acquiring new customers."
        ),
        evidence={
            "count": count,
            "sample_names": sample_names,
            "cutoff_days": 90,
        },
        evidence_count=count,
        suggested_action={
            "action": "campaign",
            "label": f"Re-engage {count} inactive customers",
            "segment_filter": {"lifecycle_stage": "customer", "inactive_days": 90},
        },
    )


def _inactive_customers_fallback(account, cutoff):
    """Fallback when orders relation is not available via annotation."""
    from apps.contacts.models import Contact

    return Contact.objects.filter(
        account=account,
        lifecycle_stage__in=["customer", "repeat_customer"],
        last_engaged_at__lt=cutoff,
    )


def rule_lead_followup_gap(account):
    """Qualified leads that received no outbound response within 24 hours."""
    from apps.conversations.state import average_first_response_seconds, needs_attention

    avg = average_first_response_seconds(account, limit=200)
    if avg is None:
        avg = 0

    # Conversations currently waiting on the agent as a proxy for un-followed-up leads
    waiting_qs = needs_attention(account, timezone.now())
    overdue = waiting_qs.filter(
        messages__created_at__lt=timezone.now() - timedelta(hours=24)
    ).distinct()
    count = overdue.count()

    if count == 0:
        return None

    avg_hours = round(avg / 3600, 1) if avg else None

    from apps.insights.models import Insight

    return Insight(
        account=account,
        type="lead_followup_gap",
        severity=Insight.Severity.WARNING,
        title=f"{count} lead{'s' if count != 1 else ''} waiting more than 24 hours",
        body=(
            f"{count} customer conversation{'s' if count != 1 else ''} "
            f"{'have' if count != 1 else 'has'} been waiting for a response for over 24 hours. "
            + (f"Your average first response is {avg_hours}h. " if avg_hours else "")
            + "Unanswered leads lose confidence quickly."
        ),
        evidence={
            "count": count,
            "avg_first_response_hours": avg_hours,
            "threshold_hours": 24,
        },
        evidence_count=count,
        suggested_action={
            "action": "follow_up",
            "label": f"Follow up with {count} waiting conversation{'s' if count != 1 else ''}",
            "filter": "overdue_24h",
        },
    )


def rule_unanswered_conversations(account):
    """Conversations waiting on agent for more than 2 hours."""
    from apps.conversations.state import OVERDUE_WAITING, needs_attention

    waiting_qs = needs_attention(account, timezone.now())
    overdue_qs = waiting_qs.filter(
        messages__created_at__lt=timezone.now() - OVERDUE_WAITING
    ).distinct()
    count = overdue_qs.count()

    if count == 0:
        return None

    from apps.insights.models import Insight

    return Insight(
        account=account,
        type="unanswered_conversations",
        severity=Insight.Severity.URGENT if count >= 5 else Insight.Severity.WARNING,
        title=f"{count} unanswered conversation{'s' if count != 1 else ''} need attention",
        body=(
            f"{count} conversation{'s are' if count != 1 else ' is'} waiting for a response "
            f"and {'have' if count != 1 else 'has'} been for more than 2 hours."
        ),
        evidence={"count": count, "threshold_hours": 2},
        evidence_count=count,
        suggested_action={
            "action": "inbox",
            "label": "Open inbox to respond",
            "filter": "needs_attention",
        },
    )


def overdue_repurchase_contacts(account) -> tuple[list, int]:
    """Return ``(overdue_rows, median_interval_days)`` for contacts past their repurchase window.

    Shared between ``rule_campaign_opportunity`` (insight generation) and the policy
    executor (trigger evaluation for ``repurchase_due`` policies).  Keeping the logic
    here ensures both callers use the same definition of "repurchase due."

    ``overdue_rows`` — list of order-row dicts (one per overdue contact) with keys:
        contact_id, paid_at, contact__first_name, contact__last_name, contact__phone
    ``median_interval_days`` — the computed (or fallback) repurchase window in days.
    Falls back to 30 days when there is insufficient order history.
    Returns ``([], 30)`` when there are no paid orders.
    """
    from apps.commerce.models import Order

    rows = list(
        Order.objects.filter(account=account, status=Order.Status.PAID)
        .values(
            "contact_id",
            "paid_at",
            "contact__first_name",
            "contact__last_name",
            "contact__phone",
        )
        .order_by("contact_id", "-paid_at")
    )
    if not rows:
        return [], 30

    contact_orders: dict[int, list] = {}
    for row in rows:
        contact_orders.setdefault(row["contact_id"], []).append(row)

    gaps: list[float] = []
    for orders in contact_orders.values():
        dates = [o["paid_at"] for o in orders if o["paid_at"] is not None]
        dates.sort()
        for i in range(1, len(dates)):
            delta = (dates[i] - dates[i - 1]).total_seconds()
            if delta > 0:
                gaps.append(delta)

    if gaps:
        gaps.sort()
        median_gap = timedelta(seconds=gaps[len(gaps) // 2])
    else:
        median_gap = timedelta(days=30)

    interval_days = round(median_gap.total_seconds() / 86400)

    now = timezone.now()
    overdue = []
    for contact_id, orders in contact_orders.items():
        last_paid = next(
            (o["paid_at"] for o in orders if o["paid_at"] is not None), None
        )
        if last_paid is None:
            continue
        if (now - last_paid) >= median_gap:
            overdue.append(orders[0])

    return overdue, interval_days


def rule_campaign_opportunity(account):
    """Customers whose repurchase interval has elapsed — prime candidates for a re-order campaign."""
    overdue_contacts, interval_days = overdue_repurchase_contacts(account)
    count = len(overdue_contacts)
    if count == 0:
        return None

    sample_names = []
    for o in overdue_contacts[:5]:
        name = f"{o.get('contact__first_name', '') or ''} {o.get('contact__last_name', '') or ''}".strip()
        sample_names.append(name or o.get("contact__phone") or "—")

    from apps.insights.models import Insight

    return Insight(
        account=account,
        type="campaign_opportunity",
        severity=Insight.Severity.OPPORTUNITY,
        title=f"{count} customer{'s' if count != 1 else ''} ready for their next purchase",
        body=(
            f"{count} customer{'s are' if count != 1 else ' is'} past their typical repurchase "
            f"window of {interval_days} day{'s' if interval_days != 1 else ''}. "
            f"A timely campaign can turn past buyers into repeat customers."
        ),
        evidence={
            "count": count,
            "sample_names": sample_names,
            "median_interval_days": interval_days,
        },
        evidence_count=count,
        suggested_action={
            "action": "campaign",
            "label": f"Re-order campaign for {count} customer{'s' if count != 1 else ''}",
        },
    )


def rule_instagram_unanswered_intent(account):
    """Instagram comment threads with buying intent that never became a DM conversation.

    A thread lands here when intent was detected but the private-reply trigger
    either wasn't configured, fired and failed, or was suppressed.  Each such
    thread is a warm prospect the business hasn't yet reached.
    """
    try:
        from apps.instagram.models.comment import CommentThread
    except ImportError:
        return None

    cutoff = timezone.now() - timedelta(days=7)
    qs = CommentThread.objects.filter(
        instagram_account__account=account,
        intent__gt="",
        conversation__isnull=True,
        received_at__gte=cutoff,
    )
    count = qs.count()
    if count == 0:
        return None

    sample = list(
        qs.order_by("-received_at").values(
            "instagram_contact__username", "intent", "body"
        )[:5]
    )
    sample_items = [
        {
            "username": r["instagram_contact__username"] or "unknown",
            "intent": r["intent"],
            "comment": (r["body"] or "")[:80],
        }
        for r in sample
    ]

    from apps.insights.models import Insight

    return Insight(
        account=account,
        type="instagram_unanswered_intent",
        severity=Insight.Severity.OPPORTUNITY
        if count < 10
        else Insight.Severity.WARNING,
        title=f"{count} Instagram comment{'s' if count != 1 else ''} with buying intent, no DM sent",
        body=(
            f"{count} Instagram comment{'s show' if count != 1 else ' shows'} buying intent "
            f"in the past 7 days but {'have' if count != 1 else 'has'} not triggered a private reply. "
            f"Each is a warm prospect who hasn't heard from you yet."
        ),
        evidence={
            "count": count,
            "window_days": 7,
            "sample": sample_items,
        },
        evidence_count=count,
        suggested_action={
            "action": "instagram_trigger",
            "label": f"Set up a buying-intent trigger to auto-reply to {count} comment{'s' if count != 1 else ''}",
            "filter": "instagram_intent_no_dm",
        },
    )


def rule_instagram_high_engagement_contact(account):
    """Instagram contacts who have commented on 3+ threads in the past 14 days.

    Multiple comments across different posts is a strong engagement signal —
    these are warm prospects worth a proactive DM before they move on.
    """
    try:
        from apps.instagram.models.comment import CommentThread
    except ImportError:
        return None

    from django.db.models import Count

    cutoff = timezone.now() - timedelta(days=14)
    qs = (
        CommentThread.objects.filter(
            instagram_account__account=account,
            received_at__gte=cutoff,
        )
        .values("instagram_contact_id", "instagram_contact__username")
        .annotate(thread_count=Count("id"))
        .filter(thread_count__gte=3)
        .order_by("-thread_count")
    )
    rows = list(qs[:50])
    count = len(rows)
    if count == 0:
        return None

    sample_items = [
        {
            "username": r["instagram_contact__username"] or "unknown",
            "thread_count": r["thread_count"],
        }
        for r in rows[:5]
    ]

    from apps.insights.models import Insight

    return Insight(
        account=account,
        type="instagram_high_engagement_contact",
        severity=Insight.Severity.OPPORTUNITY,
        title=f"{count} highly engaged Instagram contact{'s' if count != 1 else ''} worth a DM",
        body=(
            f"{count} Instagram contact{'s have' if count != 1 else ' has'} commented on 3 or more "
            f"posts in the past 14 days. High comment frequency predicts purchase intent — "
            f"{'they are' if count != 1 else 'this person is'} prime candidates for a proactive message."
        ),
        evidence={
            "count": count,
            "window_days": 14,
            "min_threads": 3,
            "sample": sample_items,
        },
        evidence_count=count,
        suggested_action={
            "action": "instagram_dm_outreach",
            "label": f"Send a DM to {count} highly engaged contact{'s' if count != 1 else ''}",
            "filter": "instagram_high_engagement",
        },
    )


# Registry of all rules.  generate_insights iterates this list.
RULES = [
    rule_unanswered_conversations,
    rule_lead_followup_gap,
    rule_inactive_customers,
    rule_campaign_opportunity,
    rule_instagram_unanswered_intent,
    rule_instagram_high_engagement_contact,
]

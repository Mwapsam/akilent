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


# Registry of all rules.  generate_insights iterates this list.
RULES = [
    rule_unanswered_conversations,
    rule_lead_followup_gap,
    rule_inactive_customers,
]

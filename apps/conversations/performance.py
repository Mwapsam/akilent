"""Follow-up completion and team performance: the "did we act on it?" metrics.

Like ``state``, everything here is derived on demand from the spine and stores nothing, so
Insights can never disagree with the inbox. Two deliberate limits:

* ``Message`` has no author, so a reply cannot be credited to whoever typed it. Team numbers
  are therefore per *assignee*: the conversations a person owns and how quickly those were
  first answered. That is the honest definition until messages record their sender.
* Rates are ``None`` when there is nothing to measure, never 0% or 100%.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

from apps.conversations.models import Conversation, FollowUp
from apps.conversations.state import (
    ConversationState,
    calculate_response_time,
    snapshot_of,
    with_activity,
)

DEFAULT_DAYS = 30
TEAM_SAMPLE_LIMIT = 500


def followup_completion(account, *, days: int = DEFAULT_DAYS, now: datetime | None = None) -> dict:
    """Of the follow-ups that fell due in the last ``days``, how many were completed.

    ``overdue`` is every still-open follow-up already past due, whatever its age, because a
    forgotten reminder from two months ago is exactly what this metric exists to surface.
    """
    now = now or timezone.now()
    due = FollowUp.objects.filter(account=account, due_at__gte=now - timedelta(days=days), due_at__lte=now)
    total = due.count()
    completed = due.filter(done_at__isnull=False).count()
    return {
        "days": days,
        "due": total,
        "completed": completed,
        "completion_pct": round(completed / total * 100) if total else None,
        "overdue": FollowUp.objects.filter(account=account, done_at__isnull=True, due_at__lt=now).count(),
    }


def team_performance(account, *, days: int = DEFAULT_DAYS, now: datetime | None = None) -> list[dict]:
    """Per assignee: conversations owned, customers currently waiting on them, average first reply.

    Covers assigned conversations active in the last ``days``. Sorted by who has the most
    customers waiting, so the row that needs attention is first.
    """
    now = now or timezone.now()
    conversations = (
        with_activity(
            Conversation.objects.filter(
                account=account, assigned_to__isnull=False,
                last_message_at__gte=now - timedelta(days=days),
            ).select_related("assigned_to")
        )
        .order_by("-last_message_at")[:TEAM_SAMPLE_LIMIT]
    )
    rows: dict[int, dict] = {}
    for conversation in conversations:
        user = conversation.assigned_to
        row = rows.setdefault(user.pk, {
            "user": user, "name": user.get_full_name() or user.get_username(),
            "conversations": 0, "waiting": 0, "_response_seconds": [],
        })
        row["conversations"] += 1
        if snapshot_of(conversation, now).state is ConversationState.WAITING_FOR_AGENT:
            row["waiting"] += 1
        seconds = calculate_response_time(conversation)
        if seconds is not None:
            row["_response_seconds"].append(seconds)

    result = []
    for row in rows.values():
        samples = row.pop("_response_seconds")
        row["avg_first_reply_minutes"] = round(sum(samples) / len(samples) / 60) if samples else None
        result.append(row)
    return sorted(result, key=lambda r: (-r["waiting"], -r["conversations"], r["name"]))

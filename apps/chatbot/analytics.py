"""
Chatbot analytics queries.

All functions are pure DB reads — no side effects.
Aggregates are scoped to a single ChatbotConfig and a (start, end) time window.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db.models import Count, Q
from django.utils import timezone

if TYPE_CHECKING:
    from apps.chatbot.models import ChatbotConfig


def period_range(days: int) -> tuple[datetime, datetime]:
    """Return (start, end) for the last `days` days (end = now)."""
    end = timezone.now()
    start = end - timedelta(days=days)
    return start, end


def session_summary(chatbot: ChatbotConfig, start: datetime, end: datetime) -> dict:
    """Counts and rates for sessions started in the window."""
    from apps.chatbot.models import ChatSession

    qs = ChatSession.objects.filter(
        chatbot=chatbot, started_at__gte=start, started_at__lt=end
    )
    total = qs.count()
    identified = qs.filter(contact__isnull=False).count()
    with_conversation = qs.filter(conversation__isnull=False).count()
    rate = round(identified / total * 100) if total else 0
    return {
        "total": total,
        "identified": identified,
        "with_conversation": with_conversation,
        "identification_rate": rate,
    }


def action_summary(
    chatbot: ChatbotConfig, start: datetime, end: datetime
) -> list[dict]:
    """Per-action execution counts for the window, ordered by total descending."""
    from apps.chatbot.models import ChatActionExecution

    rows = (
        ChatActionExecution.objects.filter(
            action__chatbot=chatbot,
            created_at__gte=start,
            created_at__lt=end,
        )
        .values("action__slug", "action__label")
        .annotate(
            total=Count("pk"),
            success=Count("pk", filter=Q(status="success")),
            failed=Count("pk", filter=Q(status="failed")),
            denied=Count("pk", filter=Q(status="denied")),
        )
        .order_by("-total")
    )
    result = []
    for r in rows:
        total = r["total"]
        success = r["success"]
        result.append(
            {
                "slug": r["action__slug"],
                "label": r["action__label"],
                "total": total,
                "success": success,
                "failed": r["failed"],
                "denied": r["denied"],
                "success_rate": round(success / total * 100) if total else 0,
            }
        )
    return result


def tickets_from_chat(chatbot: ChatbotConfig, start: datetime, end: datetime) -> int:
    """Count of support tickets that originated from a website-chat session on this chatbot.

    Proxy: count CREATED_FROM_CONVERSATION events on this account's tickets where
    channel='website_chat' and the originating conversation is linked to this chatbot.
    """
    from apps.support.models import SupportEvent

    return SupportEvent.objects.filter(
        ticket__account=chatbot.account,
        event_type=SupportEvent.CREATED_FROM_CONVERSATION,
        metadata__channel="website_chat",
        created_at__gte=start,
        created_at__lt=end,
    ).count()

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


# ── Chatbot Studio P5: outcomes ────────────────────────────────────────────────
#
# Every number comes from records that exist (sessions, messages, leads, orders linked to the
# chat's conversation), never from estimates. A chat counts in the window it started in.


def _sessions(chatbot: ChatbotConfig, start: datetime, end: datetime):
    from apps.chatbot.models import ChatSession

    return ChatSession.objects.filter(
        chatbot=chatbot, started_at__gte=start, started_at__lt=end
    )


def funnel(chatbot: ChatbotConfig, start: datetime, end: datetime) -> list[dict]:
    """``[{"key", "label", "count", "rate"}]`` from the chat loading to a paid order.

    ``rate`` is the share of the step before, in whole percent (None for the first step). Each
    step counts its own fact, so it can be checked against Leads and Orders: a lead can come
    from a chat whose visitor never left details, so a step can exceed the one before.
    """
    from apps.crm.models import Lead

    chats = _sessions(chatbot, start, end)
    wrote = chats.filter(conversation__messages__direction="inbound").distinct()
    details = chats.filter(
        Q(visitor_email__gt="")
        | Q(visitor_phone__gt="")
        | Q(contact__email__gt="")
        | Q(contact__phone__gt="")
    ).distinct()
    leads = chats.filter(conversation__leads__isnull=False).distinct()
    qualified = chats.filter(
        conversation__leads__status__in=[
            Lead.Status.QUALIFIED,
            Lead.Status.CONVERTED,
        ]
    ).distinct()
    paid = chats.filter(conversation__orders__status="paid").distinct()
    steps = [
        ("loaded", "Chat loaded on a page", chats.count()),
        ("opened", "Opened the chat", chats.filter(opened_at__isnull=False).count()),
        ("wrote", "Wrote a message", wrote.count()),
        ("details", "Left an email or phone", details.count()),
        ("lead", "Became a lead", leads.count()),
        ("qualified", "Qualified", qualified.count()),
        ("paid", "Paid an order", paid.count()),
    ]
    out, previous = [], None
    for key, label, count in steps:
        rate = round(count / previous * 100) if previous and count <= previous else None
        out.append({"key": key, "label": label, "count": count, "rate": rate})
        previous = count
    return out


def revenue(chatbot: ChatbotConfig, start: datetime, end: datetime) -> list[dict]:
    """Paid orders from chats that started in the window: ``[{"currency", "total", "orders"}]``."""
    from django.db.models import Sum

    from apps.commerce.models import Order

    return list(
        Order.objects.filter(
            status=Order.Status.PAID,
            conversation__chat_session__in=_sessions(chatbot, start, end),
        )
        .values("currency")
        .annotate(total=Sum("total"), orders=Count("pk"))
        .order_by("currency")
    )


def handoffs(chatbot: ChatbotConfig, start: datetime, end: datetime) -> dict:
    """Chats handed to the team in the window, why, how fast a person replied, and outcomes.

    ``{"total", "reasons": [{"reason", "label", "count"}], "replied", "median_reply_minutes",
    "leads", "paid"}``. The reply time is from the hand-off to the first message a person
    sent (not the bot's own "someone will reply" note).
    """
    from apps.chatbot.services.conversation import _who
    from apps.chatbot.services.handoff import reason_label
    from apps.conversations.models import Message

    handed = list(
        _handed(chatbot, start, end).values(
            "pk", "handoff_at", "handoff_reason", "conversation_id"
        )[:1000]
    )
    reasons: dict[str, int] = {}
    for row in handed:
        reasons[row["handoff_reason"]] = reasons.get(row["handoff_reason"], 0) + 1
    waits = []
    for row in handed:
        if not row["conversation_id"]:
            continue
        for ts, direction, meta in (
            Message.objects.filter(
                conversation_id=row["conversation_id"],
                direction=Message.Direction.OUTBOUND,
                timestamp__gte=row["handoff_at"],
            )
            .order_by("timestamp", "pk")
            .values_list("timestamp", "direction", "metadata")[:20]
        ):
            if _who({"direction": direction, "metadata": meta}) == "team":
                waits.append((ts - row["handoff_at"]).total_seconds() / 60)
                break
    waits.sort()
    median = None
    if waits:
        mid = len(waits) // 2
        median = waits[mid] if len(waits) % 2 else (waits[mid - 1] + waits[mid]) / 2
    conversation_ids = [r["conversation_id"] for r in handed if r["conversation_id"]]
    from apps.commerce.models import Order
    from apps.crm.models import Lead

    return {
        "total": len(handed),
        "reasons": sorted(
            (
                {"reason": k, "label": reason_label(k), "count": v}
                for k, v in reasons.items()
            ),
            key=lambda r: -r["count"],
        ),
        "replied": len(waits),
        "median_reply_minutes": round(median) if median is not None else None,
        "leads": Lead.objects.filter(conversation_id__in=conversation_ids)
        .values("conversation_id")
        .distinct()
        .count(),
        "paid": Order.objects.filter(
            conversation_id__in=conversation_ids, status=Order.Status.PAID
        )
        .values("conversation_id")
        .distinct()
        .count(),
    }


def unanswered(
    chatbot: ChatbotConfig, start: datetime, end: datetime, *, limit: int = 10
) -> list[dict]:
    """The latest visitor questions the bot's AI couldn't answer: ``[{"text", "label",
    "conversation_id", "at"}]``. Questions for a person ("asked") are left out: those weren't
    gaps in the Knowledge."""
    from apps.chatbot.services.handoff import reason_label
    from apps.conversations.models import Message

    rows = (
        Message.objects.filter(
            conversation__chat_session__chatbot=chatbot,
            direction=Message.Direction.INBOUND,
            timestamp__gte=start,
            timestamp__lt=end,
            metadata__chatbot_unanswered__isnull=False,
        )
        .exclude(metadata__chatbot_unanswered="asked")
        .select_related("conversation")
        .order_by("-timestamp", "-pk")[:limit]
    )
    return [
        {
            "text": m.body,
            "label": reason_label((m.metadata or {}).get("chatbot_unanswered", "")),
            "conversation_id": m.conversation.public_id,
            "at": m.timestamp,
        }
        for m in rows
    ]


def _handed(chatbot: ChatbotConfig, start: datetime, end: datetime):
    from apps.chatbot.models import ChatSession

    return ChatSession.objects.filter(
        chatbot=chatbot, handoff_at__gte=start, handoff_at__lt=end
    )

"""Public reporting API of conversations. Which conversations made money: the funnel and revenue by channel.

Everything is derived from the links fixed at creation (``Lead/Deal/Order.conversation``, see
``attribution``); nothing is stored. Revenue means *paid orders* only, since that is money in
hand; an open deal's value is a forecast and a won deal usually has an order behind it, so adding
them would count the same sale twice. Money is never summed across currencies.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from apps.commerce.models import Order
from apps.conversations.models import Conversation, Message
from apps.crm.models import Deal, Lead

DEFAULT_DAYS = 30
UNATTRIBUTED = "none"


def funnel(account, *, days: int = DEFAULT_DAYS, now: datetime | None = None) -> dict:
    """Customers who wrote in, then what became of them, over the last ``days``.

    Each stage counts only records that came from a conversation, so the steps read as one
    story: wrote in -> tracked as interested -> in the pipeline -> won -> paid.
    """
    since = (now or timezone.now()) - timedelta(days=days)
    wrote_in = (
        Message.objects.filter(account=account, direction=Message.Direction.INBOUND, timestamp__gte=since)
        .values("conversation__contact").distinct().count()
    )
    return {
        "days": days,
        "wrote_in": wrote_in,
        "leads": Lead.objects.filter(account=account, conversation__isnull=False, created_at__gte=since).count(),
        "deals": Deal.objects.filter(account=account, conversation__isnull=False, created_at__gte=since).count(),
        "won": Deal.objects.filter(
            account=account, conversation__isnull=False, status=Deal.Status.WON, closed_at__gte=since).count(),
        "paid": Order.objects.filter(
            account=account, conversation__isnull=False, status=Order.Status.PAID, paid_at__gte=since).count(),
    }


def revenue_by_channel(account, *, days: int = DEFAULT_DAYS, now: datetime | None = None) -> list[dict]:
    """Paid orders in the last ``days``, grouped by the channel that led to them and currency.

    Orders with no conversation behind them appear as their own "not from a conversation" rows,
    so the owner sees how much money the inbox did *not* touch, not only how much it did.
    """
    since = (now or timezone.now()) - timedelta(days=days)
    rows = (
        Order.objects.filter(account=account, status=Order.Status.PAID, paid_at__gte=since)
        .values("conversation__channel", "currency")
        .annotate(orders=Count("id"), total=Sum("total"))
        .order_by("currency", "-total")
    )
    labels = dict(Conversation.Channel.choices)
    result = []
    for row in rows:
        channel = row["conversation__channel"] or UNATTRIBUTED
        result.append({
            "channel": channel,
            "label": labels.get(channel, "Not from a conversation"),
            "currency": row["currency"],
            "orders": row["orders"],
            "total": row["total"] or Decimal("0"),
            "attributed": channel != UNATTRIBUTED,
        })
    return result

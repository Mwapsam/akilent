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


def assistant_context(conversation, *, recent: int = 8) -> dict:
    """Everything an assistant (AI or otherwise) may know about one conversation, and nothing more.

    Only this conversation and its own customer: the last ``recent`` customer and business messages
    (never system lines or internal notes), the customer's first name and tags, whether they're
    tracked as interested, whether a normal reply is allowed right now, the business's approved
    WhatsApp templates and its opening hours.
    """
    from apps.accounts import business_hours
    from apps.crm.models import Lead
    from apps.whatsapp.models import MessageTemplate

    account = conversation.account
    contact = conversation.contact
    thread = list(
        conversation.messages.filter(direction__in=[Message.Direction.INBOUND, Message.Direction.OUTBOUND])
        .exclude(body="").order_by("-timestamp", "-id").values("id", "direction", "body")[:recent]
    )[::-1]
    window_open = True
    if conversation.channel == Conversation.Channel.WHATSAPP:
        wa = conversation.whatsapp_conversation
        window_open = bool(wa and wa.window_is_open)
    templates = [
        {"name": t.whatsapp_template_name, "body": t.content or "", "blanks": list(t.variables or [])}
        for t in MessageTemplate.objects.filter(
            account=account, approval_status=MessageTemplate.ApprovalStatus.APPROVED).order_by("name")
        if t.whatsapp_template_name
    ] if conversation.channel == Conversation.Channel.WHATSAPP else []
    hours = business_hours.get_hours(account)
    hours_text = ""
    if hours and hours.schedule:
        hours_text = "; ".join(
            f"{day.title()} {slot.get('open')}-{slot.get('close')}" for day, slot in hours.schedule.items()
        ) + f" ({hours.timezone})"
    from apps.contacts.models import Tag

    return {
        "business_name": account.company_name or "",
        "hours_text": hours_text,
        # The business's own tags: AI may suggest one of these, never invent a new one.
        "business_tags": list(Tag.objects.filter(account=account).order_by("name").values_list("name", flat=True)[:40]),
        "thread": thread,
        "window_open": window_open,
        "templates": templates,
        "customer": {
            "first_name": (contact.first_name or "").strip(),
            "tags": list(contact.tags.values_list("name", flat=True)[:10]),
            "interested": Lead.objects.filter(
                account=account, contact=contact,
                status__in=[Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED]).exists(),
        },
    }


def get_conversation_by_id(conversation_id):
    """The conversation with this primary key (account preloaded), or None. For background tasks."""
    return Conversation.objects.select_related("account", "contact").filter(pk=conversation_id).first()


def earlier_messages(conversation, *, keep_recent: int, after_id: int = 0, limit: int = 40) -> list[dict]:
    """Customer and business messages older than the last ``keep_recent``, oldest first.

    Only those with an id above ``after_id`` (what a summary already covers), at most ``limit``.
    System lines and internal notes are never included. ``[{"id", "direction", "body"}]``.
    """
    talk = conversation.messages.filter(
        direction__in=[Message.Direction.INBOUND, Message.Direction.OUTBOUND]).exclude(body="")
    recent_ids = list(talk.order_by("-timestamp", "-id").values_list("id", flat=True)[:keep_recent])
    return list(
        talk.exclude(id__in=recent_ids).filter(id__gt=after_id)
        .order_by("timestamp", "id").values("id", "direction", "body")[:limit]
    )


def newest_message_ids(conversation) -> tuple[int | None, int | None]:
    """``(newest inbound id, newest outbound id)`` in this conversation, by arrival order."""
    newest = lambda d: conversation.messages.filter(direction=d).order_by("-id").values_list("id", flat=True).first()  # noqa: E731
    return newest(Message.Direction.INBOUND), newest(Message.Direction.OUTBOUND)

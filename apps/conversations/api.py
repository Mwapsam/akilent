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
        Message.objects.filter(
            account=account, direction=Message.Direction.INBOUND, timestamp__gte=since
        )
        .values("conversation__contact")
        .distinct()
        .count()
    )
    return {
        "days": days,
        "wrote_in": wrote_in,
        "leads": Lead.objects.filter(
            account=account, conversation__isnull=False, created_at__gte=since
        ).count(),
        "deals": Deal.objects.filter(
            account=account, conversation__isnull=False, created_at__gte=since
        ).count(),
        "won": Deal.objects.filter(
            account=account,
            conversation__isnull=False,
            status=Deal.Status.WON,
            closed_at__gte=since,
        ).count(),
        "paid": Order.objects.filter(
            account=account,
            conversation__isnull=False,
            status=Order.Status.PAID,
            paid_at__gte=since,
        ).count(),
    }


def revenue_by_channel(
    account, *, days: int = DEFAULT_DAYS, now: datetime | None = None
) -> list[dict]:
    """Paid orders in the last ``days``, grouped by the channel that led to them and currency.

    Orders with no conversation behind them appear as their own "not from a conversation" rows,
    so the owner sees how much money the inbox did *not* touch, not only how much it did.
    """
    since = (now or timezone.now()) - timedelta(days=days)
    rows = (
        Order.objects.filter(
            account=account, status=Order.Status.PAID, paid_at__gte=since
        )
        .values("conversation__channel", "currency")
        .annotate(orders=Count("id"), total=Sum("total"))
        .order_by("currency", "-total")
    )
    labels = dict(Conversation.Channel.choices)
    result = []
    for row in rows:
        channel = row["conversation__channel"] or UNATTRIBUTED
        result.append(
            {
                "channel": channel,
                "label": labels.get(channel, "Not from a conversation"),
                "currency": row["currency"],
                "orders": row["orders"],
                "total": row["total"] or Decimal("0"),
                "attributed": channel != UNATTRIBUTED,
            }
        )
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
        conversation.messages.filter(
            direction__in=[Message.Direction.INBOUND, Message.Direction.OUTBOUND]
        )
        .exclude(body="")
        .order_by("-timestamp", "-id")
        .values("id", "direction", "body")[:recent]
    )[::-1]
    window_open = conversation_window_is_open(conversation)
    templates = (
        [
            {
                "name": t.whatsapp_template_name,
                "body": t.content or "",
                "blanks": list(t.variables or []),
            }
            for t in MessageTemplate.objects.filter(
                account=account, approval_status=MessageTemplate.ApprovalStatus.APPROVED
            ).order_by("name")
            if t.whatsapp_template_name
        ]
        if conversation.channel == Conversation.Channel.WHATSAPP
        else []
    )
    hours = business_hours.get_hours(account)
    hours_text = ""
    if hours and hours.schedule:
        hours_text = (
            "; ".join(
                f"{day.title()} {slot.get('open')}-{slot.get('close')}"
                for day, slot in hours.schedule.items()
            )
            + f" ({hours.timezone})"
        )
    from apps.contacts.models import Tag

    return {
        "business_name": account.company_name or "",
        "hours_text": hours_text,
        # The business's own tags: AI may suggest one of these, never invent a new one.
        "business_tags": list(
            Tag.objects.filter(account=account)
            .order_by("name")
            .values_list("name", flat=True)[:40]
        ),
        "thread": thread,
        "window_open": window_open,
        "templates": templates,
        "customer": {
            "first_name": (contact.first_name or "").strip(),
            "tags": list(contact.tags.values_list("name", flat=True)[:10]),
            "interested": Lead.objects.filter(
                account=account,
                contact=contact,
                status__in=[
                    Lead.Status.NEW,
                    Lead.Status.CONTACTED,
                    Lead.Status.QUALIFIED,
                ],
            ).exists(),
        },
    }


def get_conversation_by_id(conversation_id):
    """The conversation with this primary key (account preloaded), or None. For background tasks."""
    return (
        Conversation.objects.select_related("account", "contact")
        .filter(pk=conversation_id)
        .first()
    )


def get_conversation_by_public_id(public_id: str):
    """The conversation with this public id (``conv_…``), or None. For operator tools."""
    return (
        Conversation.objects.select_related("account", "contact")
        .filter(public_id=public_id)
        .first()
    )


def conversation_for_channel_record(channel: str, object_id: int):
    """The inbox conversation backed by a channel record (e.g. an InstagramConversation)."""
    from apps.conversations.models import ChannelConversation

    link = (
        ChannelConversation.objects.filter(channel=channel, object_id=object_id)
        .select_related("conversation")
        .first()
    )
    return link.conversation if link else None


def archive_as_misfiled(conversation, *, actor: str) -> int:
    """Close a conversation that should never have existed, and drop its open leads.

    Reversible: nothing is deleted. Returns how many leads were marked lost.
    """
    if conversation.status == Conversation.Status.OPEN:
        conversation.close(resolution=Conversation.Resolution.SPAM, actor=actor)
    if conversation.is_unread:
        conversation.mark_read()
    return Lead.objects.filter(
        conversation=conversation,
        status__in=[Lead.Status.NEW, Lead.Status.CONTACTED, Lead.Status.QUALIFIED],
    ).update(status=Lead.Status.LOST)


def earlier_messages(
    conversation, *, keep_recent: int, after_id: int = 0, limit: int = 40
) -> list[dict]:
    """Customer and business messages older than the last ``keep_recent``, oldest first.

    Only those with an id above ``after_id`` (what a summary already covers), at most ``limit``.
    System lines and internal notes are never included. ``[{"id", "direction", "body"}]``.
    """
    talk = conversation.messages.filter(
        direction__in=[Message.Direction.INBOUND, Message.Direction.OUTBOUND]
    ).exclude(body="")
    recent_ids = list(
        talk.order_by("-timestamp", "-id").values_list("id", flat=True)[:keep_recent]
    )
    return list(
        talk.exclude(id__in=recent_ids)
        .filter(id__gt=after_id)
        .order_by("timestamp", "id")
        .values("id", "direction", "body")[:limit]
    )


def recent_customer_messages(account, *, since, limit: int = 3000) -> list[dict]:
    """Customers' messages since ``since``, newest first: ``[{"conversation_id", "body", "timestamp"}]``."""
    qs = (
        Message.objects.filter(
            account=account, direction=Message.Direction.INBOUND, timestamp__gte=since
        )
        .exclude(body="")
        .order_by("-timestamp")
        .values("conversation_id", "body", "timestamp")[:limit]
    )
    return list(qs)  # type: ignore[arg-type]


def first_reply_waits(account, *, since, now=None) -> list[float | None]:
    """For each conversation that *started* since ``since``: minutes until the business first
    really replied (by anyone or anything), or None if it still hasn't. A reply not yet due
    (under 5 minutes old) counts as 0."""
    from apps.conversations import reporting

    now = now or timezone.now()
    waits: list[float | None] = []
    for row in reporting.first_replies(account, since, now + timedelta(seconds=1)):
        if row["first_reply"] is None:
            waits.append(None if now - row["first_in"] > timedelta(minutes=5) else 0.0)
        else:
            waits.append((row["first_reply"] - row["first_in"]).total_seconds() / 60)
    return waits


def window_metrics(account, start, end) -> dict:
    """One business's numbers for conversations that *started* in ``[start, end)``.

    JSON-safe (stored in ``Benchmark.metrics``). The Starting point's keys (conversations,
    median_first_reply_minutes, unanswered, interested, paid_orders, revenue) plus the rest of
    ``reporting.period_metrics``.
    """
    from apps.conversations import reporting

    return reporting.period_metrics(account, start, end)


def activity(account, *, since) -> dict:
    """``{"conversations", "last_customer_message_at"}``: is this business still being written to?"""
    inbound = Message.objects.filter(
        account=account, direction=Message.Direction.INBOUND
    )
    return {
        "conversations": inbound.filter(timestamp__gte=since)
        .values("conversation")
        .distinct()
        .count(),
        "last_customer_message_at": inbound.order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first(),
    }


def starting_point(account, now=None) -> dict | None:
    """The Starting point card for this business, or None before it connects WhatsApp."""
    from apps.conversations import benchmarks
    from apps.whatsapp import api as whatsapp_api

    return benchmarks.card(account, whatsapp_api.connected_since(account), now)


def person_reply_pairs(account, *, since, limit: int = 3000) -> list[dict]:
    """What customers asked and how a *person* on the team answered, oldest first.

    ``[{"conversation_id", "question", "reply", "reply_id"}]`` where ``question`` is the customer's
    messages since the business last spoke, and ``reply`` a plain text reply sent by a person (not
    AI, not an automation, not a template or buttons).
    """
    messages = (
        Message.objects.filter(
            account=account,
            timestamp__gte=since,
            direction__in=[Message.Direction.INBOUND, Message.Direction.OUTBOUND],
        )
        .exclude(body="")
        .order_by("conversation_id", "timestamp", "id")
        .values("id", "conversation_id", "direction", "body", "metadata")[:limit]
    )
    pairs: list = []
    asked: list = []
    current = None
    for m in messages:
        if m["conversation_id"] != current:
            current, asked = m["conversation_id"], []
        if m["direction"] == Message.Direction.INBOUND:
            asked.append(m["body"])
            continue
        meta = m["metadata"] or {}
        by_person = (
            not meta.get("sent_by") and meta.get("message_type", "text") == "text"
        )
        if asked and by_person:
            pairs.append(
                {
                    "conversation_id": current,
                    "question": " ".join(asked)[:500],
                    "reply": m["body"],
                    "reply_id": m["id"],
                }
            )
        asked = []
    return pairs


def last_team_reply_at(conversation):
    """When the business last replied other than through AI (a person or an automation), or None."""
    from django.db.models import Q

    # Not a bare .exclude(metadata__sent_by="ai"): in SQL a missing key compares as NULL, so that
    # would also drop every message without "sent_by", i.e. every human reply.
    not_ai = Q(metadata__sent_by__isnull=True) | ~Q(metadata__sent_by="ai")
    return (
        conversation.messages.filter(direction=Message.Direction.OUTBOUND)
        .filter(not_ai)
        .order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )


def newest_message_ids(conversation) -> tuple[int | None, int | None]:
    """``(newest inbound id, newest outbound id)`` in this conversation, by arrival order."""

    def newest(d):
        return (
            conversation.messages.filter(direction=d)
            .order_by("-id")
            .values_list("id", flat=True)
            .first()
        )

    return newest(Message.Direction.INBOUND), newest(Message.Direction.OUTBOUND)


# Business Health (Insights) reads through here, like every other app's use of this app.
from apps.conversations.reporting import (  # noqa: E402, F401
    Period,
    at_risk,
    last_days,
    proof,
)


def conversation_window_is_open(conversation) -> bool:
    """Whether a free-text reply is allowed now: inside the channel's 24h window.

    WhatsApp and Instagram each track it on their channel record; any other
    channel has no window and is always open.
    """
    if conversation.channel == Conversation.Channel.WHATSAPP:
        wa = conversation.whatsapp_conversation
        return bool(wa and wa.window_is_open)
    if conversation.channel == Conversation.Channel.INSTAGRAM:
        ig = conversation.instagram_conversation
        return bool(ig and ig.window_is_open)
    return True

"""Which conversation led to this lead, deal or order?

Deterministic and stored: the link is fixed when the record is created (``Lead.conversation``,
``Deal.conversation``, ``Order.conversation``), so revenue reports never change because a customer
wrote again later. A record made from a conversation names it explicitly; anything else falls back
to the customer's most recent conversation in which *they* spoke within ``WINDOW_DAYS`` before the
record existed. A customer who never wrote in, or last wrote long ago, is left unattributed rather
than guessed at.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.db.models import Max
from django.utils import timezone

from apps.conversations.models import Conversation, Message

WINDOW_DAYS = 30


def resolve_conversation(account, contact, *, at: datetime | None = None, public_id: str = ""):
    """The conversation to credit for something created for ``contact`` at ``at`` (default now)."""
    if public_id:
        named = Conversation.objects.filter(account=account, contact=contact, public_id=public_id).first()
        if named is not None:
            return named
    at = at or timezone.now()
    candidates = (
        Message.objects.filter(
            account=account, conversation__contact=contact,
            direction=Message.Direction.INBOUND,
            timestamp__lte=at, timestamp__gte=at - timedelta(days=WINDOW_DAYS),
        )
        .values("conversation").annotate(last=Max("timestamp")).order_by("-last")[:1]
    )
    for row in candidates:
        return Conversation.objects.filter(pk=row["conversation"]).first()
    return None

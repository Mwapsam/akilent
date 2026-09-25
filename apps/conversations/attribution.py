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


def decide(account, contact, *, public_id: str = ""):
    """``(conversation, method)`` for something created for ``contact``; ``(None, None)`` if no evidence."""
    from apps.conversations.models import ConversationAttribution as A

    named = resolve_conversation(account, contact, public_id=public_id) if public_id else None
    if named is not None and named.public_id == public_id:
        return named, A.Method.EXPLICIT
    recent = resolve_conversation(account, contact)
    return (recent, A.Method.RECENT_CONVERSATION) if recent is not None else (None, None)


def record(obj, conversation, method, *, workflow_run=None, metadata: dict | None = None):
    """Write the immutable attribution for a lead, deal or order and set its shortcut field.

    Returns None when there is no conversation to credit. Safe to call twice: an object that
    already has a record keeps it.
    """
    from apps.conversations.models import ConversationAttribution

    if conversation is None:
        return None
    subject = type(obj)._meta.model_name  # "lead" | "deal" | "order"
    existing = ConversationAttribution.objects.filter(**{subject: obj}).first()
    if existing is not None:
        return existing
    attribution = ConversationAttribution.objects.create(
        account=obj.account, conversation=conversation, channel=conversation.channel, method=method,
        workflow_run=workflow_run, metadata=metadata or {}, **{subject: obj},
    )
    if obj.conversation_id != conversation.id:
        obj.conversation = conversation
        obj.save(update_fields=["conversation"])
    return attribution

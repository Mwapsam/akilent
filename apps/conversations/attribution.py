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


def recent_choices(account, *, limit: int = 30) -> list[dict]:
    """Conversations to offer in a "which chat led to this?" picker, newest first.

    Only conversations where the customer actually wrote in are offered: crediting a chat the
    customer never spoke in would be a guess.
    """
    rows = (
        Conversation.objects.filter(account=account, messages__direction=Message.Direction.INBOUND)
        .select_related("contact").distinct().order_by("-last_message_at", "-id")[:limit]
    )
    return [{
        "public_id": c.public_id,
        "customer": c.contact.full_name or c.contact.phone or c.contact.email or "Customer",
        "channel": c.get_channel_display(),
        "last_message_at": c.last_message_at,
        "is_open": c.status == Conversation.Status.OPEN,
    } for c in rows]


class PickerError(ValueError):
    """The chosen conversation cannot be used, worded for the person filling in the form."""


def picked_conversation(account, contact, public_id: str):
    """The conversation a person chose in a form, checked against the customer.

    Returns ``(conversation, contact)``. With no ``contact`` the conversation's own customer is
    used; naming both requires them to match, so credit can never go to someone else's chat.
    """
    public_id = (public_id or "").strip()
    if not public_id:
        return None, contact
    conversation = Conversation.objects.filter(account=account, public_id=public_id).select_related("contact").first()
    if conversation is None:
        raise PickerError("That conversation could not be found.")
    if contact is not None and conversation.contact_id != contact.id:
        raise PickerError("That conversation belongs to a different customer.")
    return conversation, conversation.contact

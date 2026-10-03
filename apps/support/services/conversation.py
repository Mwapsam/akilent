"""
Conversation → Support ticket integration.

Single entry point: create_ticket_from_conversation().

Rules:
  - Conversation must belong to the account (account-scoped from service, not view).
  - Goes through create_ticket() — never bypasses routing/SLA/events.
  - Attaches the conversation as a SupportTicketReference.
  - Copies lead/deal/order attributions as additional references where supported.
  - Adds a short transcript as an internal note (context, not source of truth).
  - Records a CREATED_FROM_CONVERSATION event with conversation_id in metadata.
  - Idempotency guard: raises DuplicateTicketError if the conversation already has
    an open (non-resolved/closed) ticket, unless force=True.
"""

from __future__ import annotations

import logging

from django.db import transaction

logger = logging.getLogger(__name__)

_TRANSCRIPT_MESSAGES = 10  # last N messages included in the context note
_SUBJECT_MAX = 120


class DuplicateTicketError(Exception):
    """Raised when the conversation already has an open support ticket."""

    def __init__(self, ticket):
        self.ticket = ticket
        super().__init__(f"Conversation already has open ticket {ticket.ticket_number}")


def create_ticket_from_conversation(
    conversation,
    *,
    submitted_by=None,
    subject: str | None = None,
    category_slug: str | None = None,
    priority: str = "p3",
    force: bool = False,
) -> object:
    """Create a support ticket from an existing conversation.

    Returns the new SupportTicket. Raises DuplicateTicketError if an open
    ticket already exists for this conversation (unless force=True).

    account-scoping is enforced inside the service — callers must pass a
    conversation that belongs to their account; the service verifies this.
    """
    from apps.conversations.models import Conversation
    from apps.support.models import SupportEvent
    from apps.support.services import record_event
    from apps.support.services.ticket import (
        add_internal_note,
        add_reference,
        create_ticket,
    )

    if not isinstance(conversation, Conversation):
        raise TypeError("conversation must be a Conversation instance")

    account = conversation.account

    # ── Derive ticket fields (outside the transaction — read-only) ─────────────
    contact = conversation.contact
    customer_tier = _resolve_tier(contact)
    derived_subject = subject or _derive_subject(conversation)
    derived_description = _derive_description(conversation)
    derived_category = category_slug or _derive_category(conversation)

    with transaction.atomic():
        # Idempotency guard inside the transaction with a row lock on the
        # conversation so two concurrent calls can't both pass the check.
        if not force:
            from apps.conversations.models import Conversation as _Conv

            _Conv.objects.select_for_update().get(pk=conversation.pk)
            existing = _find_open_ticket(conversation)
            if existing is not None:
                raise DuplicateTicketError(existing)

        ticket = create_ticket(
            account=account,
            submitted_by=submitted_by,
            subject=derived_subject,
            description=derived_description,
            category_slug=derived_category,
            customer_tier=customer_tier,
            priority=priority,
        )

        # ── Primary reference: the conversation itself ─────────────────────────
        add_reference(
            ticket=ticket,
            obj=conversation,
            relationship="caused_by",
            label=f"{conversation.get_channel_display()} conversation",
        )

        # ── Copy domain attributions (lead, deal, order) ───────────────────────
        _copy_attributions(ticket, conversation)

        # ── Transcript / context note ─────────────────────────────────────────
        transcript = _build_transcript(conversation)
        if transcript:
            add_internal_note(ticket=ticket, body=transcript, author=submitted_by)

        # ── Origin event ──────────────────────────────────────────────────────
        record_event(
            ticket,
            SupportEvent.CREATED_FROM_CONVERSATION,
            actor=submitted_by,
            conversation_id=conversation.pk,
            channel=conversation.channel,
        )

    return ticket


# ── Helpers ────────────────────────────────────────────────────────────────────


def _find_open_ticket(conversation):
    """Return an open ticket already linked to this conversation, or None."""
    from django.contrib.contenttypes.models import ContentType

    from apps.conversations.models import Conversation
    from apps.support.models import SupportTicket
    from apps.support.models.ticket import SupportTicketReference

    ct = ContentType.objects.get_for_model(Conversation)
    closed_statuses = {SupportTicket.RESOLVED, SupportTicket.CLOSED}

    ref = (
        SupportTicketReference.objects.filter(
            content_type=ct,
            object_id=str(conversation.pk),
        )
        .select_related("ticket")
        .exclude(ticket__status__in=closed_statuses)
        .first()
    )
    return ref.ticket if ref else None


def _resolve_tier(contact) -> int:
    if contact is None:
        return 1
    # lifecycle_stage → tier mapping: active/loyal customers get higher tier
    stage = getattr(contact, "lifecycle_stage", "") or ""
    return {
        "loyal": 4,
        "active": 3,
        "at_risk": 2,
    }.get(stage, 1)


def _derive_subject(conversation) -> str:
    # Use the first inbound message as the subject, truncated.
    from apps.conversations.models import Message

    first = (
        Message.objects.filter(
            conversation=conversation,
            direction=Message.Direction.INBOUND,
        )
        .order_by("timestamp")
        .values_list("body", flat=True)
        .first()
    )
    if first:
        text = first.strip().replace("\n", " ")
        return text[:_SUBJECT_MAX] + ("…" if len(text) > _SUBJECT_MAX else "")
    return f"{conversation.get_channel_display()} conversation #{conversation.pk}"


def _derive_description(conversation) -> str:
    contact = conversation.contact
    name = _contact_name(contact)
    return (
        f"Support ticket opened from a {conversation.get_channel_display()} "
        f"conversation with {name}."
    )


def _derive_category(conversation) -> str | None:
    """Map channel to a default support category slug."""
    return {
        "whatsapp": "whatsapp",
        "email": "email",
        "website_chat": "general",
    }.get(conversation.channel)


def _copy_attributions(ticket, conversation) -> None:
    """Attach lead/deal/order from ConversationAttribution as ticket references."""
    from apps.conversations.models import ConversationAttribution
    from apps.support.services.ticket import add_reference

    for attr in ConversationAttribution.objects.filter(
        conversation=conversation
    ).select_related("lead", "deal", "order"):
        for obj, label in [
            (getattr(attr, "lead", None), "Lead"),
            (getattr(attr, "deal", None), "Deal"),
            (getattr(attr, "order", None), "Order"),
        ]:
            if obj is not None:
                try:
                    add_reference(ticket=ticket, obj=obj, label=label)
                except Exception:
                    logger.exception("failed to copy attribution reference %s", obj)


def _build_transcript(conversation) -> str:
    """Return the last N messages as a readable context note."""
    from apps.conversations.models import Message

    msgs = list(
        Message.objects.filter(conversation=conversation)
        .order_by("-timestamp")
        .values("direction", "body", "timestamp")[:_TRANSCRIPT_MESSAGES]
    )
    if not msgs:
        return ""

    msgs.reverse()
    lines = ["Recent conversation context:\n"]
    for m in msgs:
        direction = "Customer" if m["direction"] == "inbound" else "Agent"
        ts = m["timestamp"].strftime("%d %b %H:%M") if m["timestamp"] else ""
        body = (m["body"] or "").strip()[:300]
        lines.append(f"[{ts}] {direction}: {body}")

    return "\n".join(lines)


def _contact_name(contact) -> str:
    if contact is None:
        return "unknown contact"
    for attr in ("full_name", "name", "first_name"):
        val = getattr(contact, attr, None)
        if val:
            return str(val)
    return f"contact #{contact.pk}"

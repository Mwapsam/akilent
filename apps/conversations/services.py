"""Phase 1 spine service: turns an inbound WhatsApp message into the generic
Conversation/Message/Event primitives, then enrolls Workflows.

This is the adapter boundary described in the plan: WhatsApp's existing
implementation (webhook -> WhatsAppContact -> whatsapp.Conversation ->
MessageLog) is untouched. This module is called *after* that pipeline has
already run, using the already-resolved WhatsApp records, and never knows
about WhatsApp beyond receiving them as arguments — the workflow trigger it
raises (``conversation.message_received``) is channel-agnostic.
"""
from __future__ import annotations

import logging
from datetime import datetime

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.conversations import metrics
from apps.conversations.models import Conversation, Event, Message
from apps.core.actions import run_action

logger = logging.getLogger(__name__)


def emit_event(
    *, account, type: str, occurred_at: datetime, source: str,
    source_event_id: str = "", payload: dict | None = None,
    actor: str = "", subject_type: str = "", subject_id: str = "",
    correlation_id: str = "",
) -> Event | None:
    """Idempotently record a durable domain Event.

    Returns ``None`` (rather than raising) if this exact ``(account, source,
    source_event_id)`` was already recorded — the same shape as
    ``MessageLog.get_or_create`` idempotency elsewhere in the codebase.
    """
    try:
        with transaction.atomic():
            return Event.objects.create(
                account=account, type=type, occurred_at=occurred_at,
                source=source, source_event_id=source_event_id,
                payload=payload or {}, actor=actor,
                subject_type=subject_type, subject_id=subject_id,
                correlation_id=correlation_id,
            )
    except IntegrityError:
        logger.info(
            "emit_event: duplicate event type=%s source=%s source_event_id=%s",
            type, source, source_event_id,
        )
        return None


def record_inbound_whatsapp_message(
    *, contact, wa_contact, whatsapp_conversation, message_log, enroll_workflows: bool = True,
) -> Conversation | None:
    """Project an already-recorded inbound WhatsApp message onto the spine.

    ``contact`` is the resolved ``apps.contacts.Contact`` (never the
    WhatsApp-specific identity). Returns the generic ``Conversation``, or
    ``None`` if this message was already projected (e.g. a replayed
    webhook) — in which case no duplicate Event/Workflow enrollment happens.

    ``enroll_workflows=False`` records the Inbox conversation/message without
    starting any Workflow (used when automation events are switched off).
    """
    conversation = Conversation.get_or_create_for_whatsapp(whatsapp_conversation)
    conversation.register_inbound(message_log.timestamp)

    message, created = Message.objects.get_or_create(
        whatsapp_message=message_log,
        defaults={
            "account": conversation.account,
            "conversation": conversation,
            "direction": Message.Direction.INBOUND,
            "body": message_log.content,
            "timestamp": message_log.timestamp,
            "status": message_log.status,
            "metadata": {"message_type": message_log.message_type},
        },
    )
    if not created:
        return None

    event = emit_event(
        account=conversation.account,
        type="conversation.message_received",
        occurred_at=message_log.timestamp,
        source="whatsapp",
        source_event_id=message_log.message_id or "",
        payload={
            "conversation_id": conversation.public_id,
            "message_id": message.id,
            "body": message_log.content,
            "message_type": message_log.message_type,
        },
        subject_type="conversation",
        subject_id=conversation.public_id,
    )
    if event is None or not enroll_workflows:
        return conversation

    try:
        _enroll_workflows(conversation, contact, message_log)
    except Exception:
        logger.exception(
            "record_inbound_whatsapp_message: workflow enrollment failed for conversation=%s",
            conversation.pk,
        )

    capture_opportunity(conversation, contact, message_log.content)
    return conversation


def record_system_message(*, account, contact, body: str, metadata: dict | None = None) -> Message | None:
    """Put a fact about the customer's story into their conversation thread.

    For things that happened outside the chat but belong to it — "Payment
    received — K450" — so the conversation stays the whole story instead of the
    business having to go and look in Orders (docs/plans — "payment result is
    reflected back into the conversation").

    Lands on the customer's most recent conversation. Returns ``None`` when
    they have none, since there's no thread to write to. A system line is
    neither party speaking, so it never changes who is waiting for whom.
    """
    conversation = (
        Conversation.objects.filter(account=account, contact=contact)
        .order_by("-last_message_at", "-created_at")
        .first()
    )
    if conversation is None:
        return None

    return Message.objects.create(
        account=account, conversation=conversation,
        direction=Message.Direction.SYSTEM, body=body,
        timestamp=timezone.now(), metadata=metadata or {},
    )


def capture_opportunity(conversation: Conversation, contact, body: str) -> None:
    """Open a lead when a customer's message asks to buy something.

    The architectural intent is that a Lead *originates from the conversation*
    the same way a Contact does, rather than being typed into a CRM — so this
    runs on every inbound message rather than waiting for an agent to press a
    button (docs/plans — "conversation-originated Lead/Order is core scope").

    Never more than one open lead per customer: a customer asking three
    questions is one opportunity, not three. Best-effort — a failure here must
    not cost us the message.
    """
    from apps.conversations.intent import detect_buying_intent

    try:
        phrase = detect_buying_intent(body)
        if phrase is None:
            return
        # Goes through the registry, so module gating ("Track potential sales"
        # switched off) and CRM's own rules apply exactly as they do for the
        # agent's manual button — and no crm model is imported here.
        run_action(
            "capture_conversation_lead", {"account": conversation.account},
            account=conversation.account, contact=contact,
            conversation_id=conversation.public_id, signal=phrase,
        )
    except Exception:
        logger.exception(
            "capture_opportunity failed for conversation=%s", conversation.pk
        )


def _enroll_workflows(conversation: Conversation, contact, message_log) -> None:
    from apps.automation.workflow_engine import enroll_for_trigger

    enroll_for_trigger(
        conversation.account_id,
        "conversation.message_received",
        contact,
        context={
            "conversation_id": conversation.public_id,
            "message": {
                "body": message_log.content,
                "type": message_log.message_type,
            },
        },
    )


# Same ordering as the provider-side log: a status only moves forward, and "failed" is terminal.
_STATUS_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 99}


def record_outbound_message(
    *, conversation: Conversation, body: str, timestamp: datetime, status: str,
    metadata: dict | None = None, whatsapp_message=None,
) -> tuple[Message, bool]:
    """Record a business message (human reply, template or workflow send) on the spine.

    Channel-neutral: the adapter passes what it knows and, optionally, its provider
    record so the call is idempotent (a replay returns the existing ``Message`` and
    carries a newer status forward). Returns ``(message, created)``. Whether it counts
    as the business having *responded* is decided by ``state.py`` from ``status``.
    """
    metrics.incr("outbound_projection_attempts")
    if whatsapp_message is not None:
        message, created = Message.objects.get_or_create(
            whatsapp_message=whatsapp_message,
            defaults={
                "account": conversation.account, "conversation": conversation,
                "direction": Message.Direction.OUTBOUND, "body": body,
                "timestamp": timestamp, "status": status, "metadata": metadata or {},
            },
        )
    else:
        message = Message.objects.create(
            account=conversation.account, conversation=conversation,
            direction=Message.Direction.OUTBOUND, body=body, timestamp=timestamp,
            status=status, metadata=metadata or {},
        )
        created = True

    if created:
        metrics.incr("outbound_projection_created")
        conversation.register_outbound(timestamp)
    else:
        metrics.incr("outbound_projection_duplicates")
        record_message_status(message, status)
        # A status-only replay can carry new information (e.g. a failure reason
        # that wasn't known when the message was first queued/sent). Merge it in
        # rather than requiring the caller to know whether this is a first write.
        if metadata:
            merged = {**message.metadata, **metadata}
            if merged != message.metadata:
                message.metadata = merged
                message.save(update_fields=["metadata"])
    return message, created


def record_message_status(message: Message, status: str) -> bool:
    """Advance a message's delivery status. Returns True if it changed."""
    incoming = _STATUS_RANK.get(status)
    if incoming is None or incoming <= _STATUS_RANK.get(message.status, 0):
        return False
    message.status = status
    message.save(update_fields=["status"])
    metrics.incr("status_updates_applied", status=status)
    return True

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

from apps.conversations.models import Conversation, Event, Message

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
    *, contact, wa_contact, whatsapp_conversation, message_log,
) -> Conversation | None:
    """Project an already-recorded inbound WhatsApp message onto the spine.

    ``contact`` is the resolved ``apps.contacts.Contact`` (never the
    WhatsApp-specific identity). Returns the generic ``Conversation``, or
    ``None`` if this message was already projected (e.g. a replayed
    webhook) — in which case no duplicate Event/Workflow enrollment happens.
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
    if event is None:
        return conversation

    try:
        _enroll_workflows(conversation, contact, message_log)
    except Exception:
        logger.exception(
            "record_inbound_whatsapp_message: workflow enrollment failed for conversation=%s",
            conversation.pk,
        )
    return conversation


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

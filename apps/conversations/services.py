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
    *,
    account,
    type: str,
    occurred_at: datetime,
    source: str,
    source_event_id: str = "",
    payload: dict | None = None,
    actor: str = "",
    subject_type: str = "",
    subject_id: str = "",
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
                account=account,
                type=type,
                occurred_at=occurred_at,
                source=source,
                source_event_id=source_event_id,
                payload=payload or {},
                actor=actor,
                subject_type=subject_type,
                subject_id=subject_id,
                correlation_id=correlation_id,
            )
    except IntegrityError:
        logger.info(
            "emit_event: duplicate event type=%s source=%s source_event_id=%s",
            type,
            source,
            source_event_id,
        )
        return None


def delete_conversation(conversation: Conversation, *, actor) -> None:
    """Delete a conversation for good: its messages, notes and form answers go with it.

    Leads, deals and orders that came from it are kept (their link is cleared). The
    WhatsApp/Instagram records stay, so if the customer writes again a fresh conversation
    starts. An Event records who deleted it and when.
    """
    with transaction.atomic():
        emit_event(
            account=conversation.account,
            type="conversation.deleted",
            occurred_at=timezone.now(),
            source="inbox",
            source_event_id=f"deleted:{conversation.public_id}",
            payload={
                "conversation_id": conversation.public_id,
                "channel": conversation.channel,
                "contact_id": conversation.contact_id,
                "messages": conversation.messages.count(),
            },
            actor=getattr(actor, "username", "") or "",
            subject_type="conversation",
            subject_id=conversation.public_id,
        )
        conversation.delete()


def record_inbound_whatsapp_message(
    *,
    contact,
    wa_contact,
    whatsapp_conversation,
    message_log,
    enroll_workflows: bool = True,
) -> Conversation | None:
    """Project an already-recorded inbound WhatsApp message onto the spine.

    ``contact`` is the resolved ``apps.contacts.Contact`` (never the
    WhatsApp-specific identity). Returns the generic ``Conversation``, or
    ``None`` if this message was already projected (e.g. a replayed
    webhook) — in which case no duplicate Event/Workflow enrollment happens.

    ``enroll_workflows=False`` records the Inbox conversation/message without
    starting any Workflow (used when automation events are switched off).
    """
    from apps.conversations.models import ChannelConversation
    from apps.whatsapp.interactive import reply_for_log

    is_new = not ChannelConversation.objects.filter(
        channel=Conversation.Channel.WHATSAPP,
        object_id=whatsapp_conversation.pk,
    ).exists()
    conversation = Conversation.get_or_create_for_whatsapp(whatsapp_conversation)
    if is_new:
        route_new_conversation(conversation)
    conversation.register_inbound(message_log.timestamp)
    reply = reply_for_log(message_log)  # a tapped button or list choice, else None

    message, created = Message.objects.get_or_create(
        whatsapp_message=message_log,
        defaults={
            "account": conversation.account,
            "conversation": conversation,
            "direction": Message.Direction.INBOUND,
            "body": message_log.content,
            "timestamp": message_log.timestamp,
            "status": message_log.status,
            "metadata": {
                "message_type": message_log.message_type,
                **({"reply": reply} if reply else {}),
            },
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

    handled = False
    if enroll_workflows:
        try:
            wa_account_id = (
                whatsapp_conversation.contact.phone_number
                if hasattr(whatsapp_conversation, "contact")
                else ""
            )
            msg_id = message_log.message_id or ""
            handled = _enroll_workflows(
                conversation,
                contact,
                {
                    "body": message_log.content,
                    "type": message_log.message_type,
                    "_conversation": conversation,
                    "_message_key": f"whatsapp:{wa_account_id}:{msg_id}",
                },
                reply,
            )
        except Exception:
            logger.exception(
                "record_inbound_whatsapp_message: workflow enrollment failed for conversation=%s",
                conversation.pk,
            )
    _announce_processed(conversation, message, handled)
    if not enroll_workflows:
        return conversation

    _record_customer_activity(contact, message_log.timestamp, message_log.content)
    _clear_obsolete_followups(conversation, contact)
    capture_opportunity(conversation, contact, message_log.content)
    return conversation


def record_inbound_instagram_message(
    *,
    contact,
    instagram_conversation,
    instagram_message,
    enroll_workflows: bool = True,
) -> Conversation | None:
    """Project an already-created inbound InstagramMessage onto the spine.

    Mirrors ``record_inbound_whatsapp_message`` step for step — routing, forms,
    wait-for-reply, keyword workflows, customer timeline, follow-ups, lead
    capture, then the AI hand-off — so a business gets the same behaviour
    whichever channel the customer writes on. Idempotent on the
    ``instagram_message`` FK: a replayed webhook returns None.
    """
    from apps.conversations.models import ChannelConversation, Conversation

    is_new = not ChannelConversation.objects.filter(
        channel=Conversation.Channel.INSTAGRAM,
        object_id=instagram_conversation.pk,
    ).exists()
    conversation = Conversation.get_or_create_for_instagram(instagram_conversation)
    if is_new:
        route_new_conversation(conversation)
    conversation.register_inbound(instagram_message.timestamp)

    message_type = (instagram_message.metadata or {}).get("message_type") or "text"
    message, created = Message.objects.get_or_create(
        instagram_message=instagram_message,
        defaults={
            "account": conversation.account,
            "conversation": conversation,
            "direction": Message.Direction.INBOUND,
            "body": instagram_message.body,
            "timestamp": instagram_message.timestamp,
            "metadata": {"message_type": message_type},
        },
    )
    if not created:
        if instagram_message.body and not message.body:
            message.body = instagram_message.body
            message.save(update_fields=["body"])
        return None

    event = emit_event(
        account=conversation.account,
        type="conversation.message_received",
        occurred_at=instagram_message.timestamp,
        source="instagram",
        source_event_id=instagram_message.message_id or "",
        payload={
            "conversation_id": conversation.public_id,
            "message_id": message.id,
            "body": instagram_message.body,
            "message_type": message_type,
        },
        subject_type="conversation",
        subject_id=conversation.public_id,
    )
    if event is None:
        return conversation

    handled = False
    if enroll_workflows:
        try:
            # Extract the IG quick_reply payload (if any) as the reply_id for
            # token-based interaction routing (A workstream).
            raw_meta = instagram_message.metadata or {}
            ig_msg_raw = raw_meta.get("message") or {}
            qr_payload = (ig_msg_raw.get("quick_reply") or {}).get("payload", "")
            ig_account_id = (
                instagram_conversation.instagram_account.instagram_business_account_id
                if instagram_conversation
                else ""
            )
            ig_mid = instagram_message.message_id or ""
            msg_dict: dict = {
                "body": instagram_message.body,
                "type": message_type,
                "_conversation": conversation,
                "_message_key": f"instagram:{ig_account_id}:{ig_mid}",
            }
            if qr_payload:
                msg_dict["reply_id"] = qr_payload
            handled = _enroll_workflows(conversation, contact, msg_dict)
        except Exception:
            logger.exception(
                "record_inbound_instagram_message: workflow enrollment failed for conversation=%s",
                conversation.pk,
            )
    _announce_processed(conversation, message, handled)
    if not enroll_workflows:
        return conversation

    _record_customer_activity(
        contact, instagram_message.timestamp, instagram_message.body
    )
    _clear_obsolete_followups(conversation, contact)
    capture_opportunity(conversation, contact, instagram_message.body)
    return conversation


def route_new_conversation(conversation: Conversation) -> None:
    """Give a conversation to a team/agent per the account's RoutingRules.

    Called from here once, only for a conversation this call just created — a
    returning customer's second message must never re-route a conversation
    someone is already handling. Also reused by the overdue-escalation task
    (``apps.conversations.tasks.escalate_overdue_conversations``) for a
    still-unassigned conversation, where re-routing *is* the point. Best-effort,
    same as the rest of this file's side effects: routing must never cost us
    the message itself.
    """
    try:
        run_action(
            "route_conversation",
            {"account": conversation.account},
            conversation=conversation,
        )
    except Exception:
        logger.exception(
            "_route_new_conversation failed for conversation=%s", conversation.pk
        )


def _record_customer_activity(contact, timestamp, body: str) -> None:
    """Put the customer's message on their own timeline, and stamp them as
    recently engaged.

    Without this a WhatsApp-first business has an empty customer history and a
    ``last_engaged_at`` that only ever moves when someone opens an email — which
    would make any "hasn't been in touch for N days" rule silently wrong.
    Best-effort: the message is already recorded, and losing its timeline entry
    must not lose the message."""
    from apps.contacts.services import record_contact_event

    try:
        record_contact_event(
            contact,
            "conversation.message_received",
            occurred_at=timestamp,
            data={"body": (body or "")[:280]},
        )
    except Exception:
        logger.exception(
            "could not record customer activity for contact=%s", contact.pk
        )


def _clear_obsolete_followups(conversation, contact) -> None:
    """A reminder to chase someone who has just written back is noise.

    The customer replying is the thing the reminder was waiting for, so the
    business shouldn't have to tick it off by hand. Only *due-in-the-future or
    overdue but still open* reminders for this customer are closed; anything
    already marked done is left alone."""
    from apps.conversations.models import FollowUp

    try:
        open_followups = FollowUp.objects.filter(
            account=conversation.account,
            contact=contact,
            done_at__isnull=True,
        )
        for followup in open_followups:
            followup.mark_done()
    except Exception:
        logger.exception("could not close follow-ups for contact=%s", contact.pk)


def record_system_message(
    *, account, contact, body: str, metadata: dict | None = None
) -> Message | None:
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
        account=account,
        conversation=conversation,
        direction=Message.Direction.SYSTEM,
        body=body,
        timestamp=timezone.now(),
        metadata=metadata or {},
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
            "capture_conversation_lead",
            {"account": conversation.account},
            account=conversation.account,
            contact=contact,
            conversation_id=conversation.public_id,
            signal=phrase,
        )
    except Exception:
        logger.exception(
            "capture_opportunity failed for conversation=%s", conversation.pk
        )


def _enroll_workflows(
    conversation: Conversation, contact, message: dict, reply: dict | None = None
) -> bool:
    """Start or resume workflows for this message. True if one resumed or answered it.

    Channel-neutral: ``message`` is ``{"body", "type"}``; ``reply`` is a tapped
    button/list choice (WhatsApp interactive), if any.
    """
    from apps.automation.workflow_engine import answer_message, resume_on_reply
    from apps.conversations import forms as conversation_forms

    message = dict(message)
    if reply:
        message["reply_id"] = reply["id"]
        message["reply_title"] = reply["title"]

    # A completed WhatsApp Flow arrives as an nfm_reply interactive message.
    # It must be handled before record_answer so the synthetic message body
    # (whatever text Meta echoes back) is never treated as a stray text answer
    # to whatever current_index a text-mode form might coincidentally be on.
    if reply and reply.get("kind") == "nfm_reply":
        if conversation_forms.complete_from_flow(
            conversation,
            reply.get("flow_token", ""),
            reply.get("fields", {}),
        ):
            return True

    # A customer answering a form question must not also start or resume an
    # unrelated workflow, or reach AI as if it were a normal message — same
    # reasoning as the wait_for_reply check just below, checked first since a
    # form in progress takes the message even if it also happens to look like
    # a workflow's wait_for_reply choice.
    if conversation_forms.record_answer(conversation, message["body"]):
        return True

    # A customer answering a question an automation asked ("Prices or Booking?") continues
    # that conversation; it must not also start unrelated keyword workflows.
    if resume_on_reply(conversation.account_id, contact, message):
        return True

    # Only a workflow that actually answered keeps AI out; one that ran and said nothing
    # (a welcome for someone already welcomed) must not silence AI for good.
    # Strip routing-only keys (prefixed with "_") before storing the message in the run context.
    stored_message = {k: v for k, v in message.items() if not k.startswith("_")}
    return answer_message(
        conversation.account_id,
        contact,
        context={"conversation_id": conversation.public_id, "message": stored_message},
    )


def _announce_processed(
    conversation: Conversation, message: Message, handled: bool
) -> None:
    """Let optional consumers (AI) react once deterministic automation has had its chance."""
    from apps.conversations.signals import conversation_message_processed

    for receiver, result in conversation_message_processed.send_robust(
        sender=Conversation,
        conversation=conversation,
        message=message,
        handled_by_automation=bool(handled),
    ):
        if isinstance(result, Exception):
            logger.error(
                "conversation_message_processed receiver %r failed: %r",
                receiver,
                result,
            )


# Same ordering as the provider-side log: a status only moves forward, and "failed" is terminal.
_STATUS_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 99}


def record_outbound_message(
    *,
    conversation: Conversation,
    body: str,
    timestamp: datetime,
    status: str,
    metadata: dict | None = None,
    whatsapp_message=None,
    instagram_message=None,
) -> tuple[Message, bool]:
    """Record a business message (human reply, template or workflow send) on the spine.

    Channel-neutral: the adapter passes what it knows and, optionally, its provider
    record so the call is idempotent (a replay returns the existing ``Message`` and
    carries a newer status forward). Returns ``(message, created)``. Whether it counts
    as the business having *responded* is decided by ``state.py`` from ``status``.
    """
    metrics.incr("outbound_projection_attempts")
    provider_link = (
        {"whatsapp_message": whatsapp_message}
        if whatsapp_message is not None
        else {"instagram_message": instagram_message}
        if instagram_message is not None
        else None
    )
    if provider_link is not None:
        message, created = Message.objects.get_or_create(
            **provider_link,
            defaults={
                "account": conversation.account,
                "conversation": conversation,
                "direction": Message.Direction.OUTBOUND,
                "body": body,
                "timestamp": timestamp,
                "status": status,
                "metadata": metadata or {},
            },
        )
    else:
        message = Message.objects.create(
            account=conversation.account,
            conversation=conversation,
            direction=Message.Direction.OUTBOUND,
            body=body,
            timestamp=timestamp,
            status=status,
            metadata=metadata or {},
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

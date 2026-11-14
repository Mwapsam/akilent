"""What happens when a website visitor opens the chat or writes in (Chatbot Studio P1).

Every visitor message is recorded through ``apps.conversations`` like any other channel's, so it
gets the inbox, the timeline, lead capture and AI suggestions. Who answers is decided here, in
this order, the same order the other channels use (``conversations.services._enroll_workflows``):

1. A teammate took the chat over (automations paused): nobody automatic answers.
2. An automation is waiting for this answer (a question it asked): it continues.
3. The bot's own workflow, when its trigger is "visitor messages the chat".
4. Otherwise the bot's built-in AI answers from its knowledge. With AI off for the business,
   the visitor is told once that someone will reply here, and the team takes it.

A website visitor never starts the account's WhatsApp/Instagram/Facebook message workflows,
and the bot's workflow never fires on those channels (its triggers are website-only).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from apps.automation.models import Workflow
    from apps.chatbot.models import ChatSession
    from apps.conversations.models import Conversation, Message

logger = logging.getLogger(__name__)

OPENED_TRIGGER = "website_chat.opened"
MESSAGE_TRIGGER = "website_chat.message_received"

HOLDING_REPLY = (
    "Thanks for your message! Someone from the team will reply here shortly."
)


def _bot_workflow(session: ChatSession, trigger_type: str) -> Workflow | None:
    """The bot's workflow, if it's on and starts from ``trigger_type``."""
    from apps.automation.models import Workflow

    workflow = session.chatbot.workflow
    if workflow is None or workflow.status != Workflow.Status.PUBLISHED:
        return None
    trigger = (workflow.definition or {}).get("trigger") or {}
    return workflow if trigger.get("type") == trigger_type else None


def _message_context(conversation: Conversation, message: Message | None) -> dict:
    context: dict = {"conversation_id": conversation.public_id}
    if message is not None:
        context["message"] = {"body": message.body, "type": "text"}
    return context


def _start_workflow(
    workflow: Workflow, session: ChatSession, conversation, message, since
) -> bool:
    """Start the bot's workflow for this visitor. True if it said something or now waits on them."""
    from apps.automation.workflow_engine import (
        enroll,
        trigger_verdict,
        waiting_on_customer,
    )
    from apps.conversations.models import Message as SpineMessage

    contact = session.contact
    context = _message_context(conversation, message)
    trigger = (workflow.definition or {}).get("trigger") or {}
    if trigger_verdict(workflow, trigger, contact, context) is not None:
        return False  # a keyword it doesn't match, or its cooldown
    run = enroll(workflow, contact, context=context)
    if run is None:
        return False
    run.refresh_from_db()
    if waiting_on_customer(run):
        return True
    return SpineMessage.objects.filter(
        conversation=conversation,
        direction=SpineMessage.Direction.OUTBOUND,
        timestamp__gte=since,
    ).exists()


def _send(conversation, body: str, key: str) -> None:
    """Say ``body`` in the chat through the shared ``reply`` action (saved, shown in the inbox)."""
    from apps.core.actions import run_action

    run_action(
        "reply",
        {"account": conversation.account},
        conversation=conversation,
        body=body,
        idempotency_key=key,
    )


def _holding_already_sent(conversation) -> bool:
    """Whether the newest bot message is already the "someone will reply" note."""
    from apps.conversations.models import Message

    last = (
        Message.objects.filter(
            conversation=conversation, direction=Message.Direction.OUTBOUND
        )
        .order_by("-timestamp", "-pk")
        .values_list("metadata", flat=True)
        .first()
    )
    return bool(last) and (last or {}).get("auto_kind") == "holding"


def handle_visitor_message(session: ChatSession, body: str) -> dict:
    """Record one visitor message, let the right party answer, and return what the widget shows.

    Returns ``{"reply", "messages", "handoff_suggested", "ticket_number", "cursor"}``: the new
    messages since the visitor's (bot or team), their text joined as ``reply`` for older widgets,
    and the cursor for ``/api/chat/poll/``.
    """
    from django.utils import timezone

    from apps.automation.workflow_engine import resume_on_reply
    from apps.chatbot.services.conversation import (
        get_or_create_for_website_chat,
        messages_after,
    )
    from apps.chatbot.services.responder import ai_reply
    from apps.conversations import services as conversation_services

    conversation = get_or_create_for_website_chat(session)
    contact = session.contact
    outcome = {"handoff": False}

    def decide(conversation, message) -> str:
        if conversation.automation_paused:
            return ""  # a teammate has it; announced as "paused" by conversations
        since = timezone.now()
        reply_message = {
            "body": message.body,
            "type": "text",
            "_conversation": conversation,
            "_message_key": f"website_chat:{message.pk}",
        }
        if resume_on_reply(conversation.account_id, contact, reply_message):
            return conversation_services.HANDLED_REPLY

        workflow = _bot_workflow(session, MESSAGE_TRIGGER)
        if workflow is not None and _start_workflow(
            workflow, session, conversation, message, since
        ):
            return conversation_services.HANDLED_AUTOMATION

        text, handoff = ai_reply(session, message.body, exclude_message_id=message.pk)
        outcome["handoff"] = handoff
        if text is None:
            # AI is off for this business: tell the visitor once, then it's the team's.
            outcome["handoff"] = True
            if not _holding_already_sent(conversation):
                from apps.conversations.inbox_keys import HOLDING_PREFIX

                _send(conversation, HOLDING_REPLY, f"{HOLDING_PREFIX}chat:{message.pk}")
            return ""
        _send(conversation, text, f"ai-auto:chatbot:{message.pk}")
        return conversation_services.HANDLED_AUTOMATION

    inbound = conversation_services.record_inbound_website_message(
        conversation=conversation, contact=contact, body=body, handle=decide
    )
    session.last_activity_at = timezone.now()
    session.save(update_fields=["last_activity_at"])

    new, cursor = messages_after(session, None, after_message_id=inbound.pk)
    return {
        "reply": "\n\n".join(m["text"] for m in new),
        "messages": new,
        "handoff_suggested": outcome["handoff"],
        "ticket_number": None,
        "cursor": cursor,
    }


def handle_opened(session: ChatSession) -> dict:
    """The visitor opened the chat window: run the bot's "opens the chat" workflow, once.

    Only a bot whose workflow starts from that trigger creates a conversation here (the owner
    chose to greet visitors with it); otherwise nothing is stored until the visitor writes.
    """
    from django.utils import timezone

    from apps.chatbot.models import ChatSession as Session
    from apps.chatbot.services.conversation import (
        get_or_create_for_website_chat,
        messages_after,
    )

    claimed = Session.objects.filter(pk=session.pk, opened_at__isnull=True).update(
        opened_at=timezone.now()
    )
    workflow = _bot_workflow(session, OPENED_TRIGGER)
    if claimed and workflow is not None:
        conversation = get_or_create_for_website_chat(session)
        try:
            _start_workflow(workflow, session, conversation, None, timezone.now())
        except Exception:
            logger.exception(
                "chatbot opened workflow failed for session=%s", session.session_key
            )
    new, cursor = messages_after(session, 0)
    return {"messages": new, "cursor": cursor}

"""Messaging/Inbox actions, registered into the shared Action Registry
(``apps.core.actions``).

Phase 1 established the registry contract here; from Phase 2 onward the
contract itself lives in ``apps.core.actions`` so other modules (``apps.crm``,
later Commerce) can register into the same registry without depending on
``apps.conversations``. Re-exported here (``Action``, ``ActionError``,
``run_action``, ...) so existing imports keep working.
"""
from __future__ import annotations

from apps.core.actions import (  # noqa: F401 — re-exported for existing imports
    Action,
    ActionError,
    available_actions,
    get_action,
    register,
    run_action,
)


class SendWhatsAppAction(Action):
    """Send a WhatsApp template message to a Contact.

    Reuses ``apps.automation.workflows.send_whatsapp_message`` — the same
    function the Workflow engine's ``send_whatsapp`` step and the legacy
    AutomationRule action call — so there remains exactly one place that
    talks to WhatsAppContact/MessageTemplate/OutboundMessage.
    """

    name = "send_whatsapp"
    scope_kwarg = "account"

    def input_schema(self) -> dict:
        return {
            "required": ["account", "phone", "template_id"],
            "optional": ["params", "scheduled_at", "conversation"],
        }

    def execute(self, context: dict, *, account, phone: str, template_id: int,
                params: dict | None = None, scheduled_at=None, conversation=None) -> dict:
        from apps.automation.workflows import send_whatsapp_message

        wa_conversation = getattr(conversation, "whatsapp_conversation", None)
        if wa_conversation is not None:
            # The conversation names the exact WhatsApp identity to reply to.
            # Matching on the canonical Contact's phone instead can resolve to a
            # different WhatsAppContact (same person, differently formatted
            # number), which sends the reply into someone else's thread.
            phone = wa_conversation.contact.phone_number or phone
        if not phone:
            raise ActionError("send_whatsapp requires a phone number")
        try:
            msg = send_whatsapp_message(
                account, phone=phone, template_id=template_id,
                params=params or {}, scheduled_at=scheduled_at,
                conversation=wa_conversation,
            )
        except ValueError as exc:
            # Unsendable template or no WhatsApp/opt-in record for this number:
            # the caller's problem to fix, so it surfaces as a message rather
            # than a 500.
            raise ActionError(str(exc)) from exc
        return {"outbound_message_id": msg.id}


class ReplyAction(Action):
    """Send a free-text reply in an open conversation.

    Channel-agnostic entry point: for Phase 1 (WhatsApp only) it resolves the
    conversation's WhatsApp identity and sends via the free-text path (valid
    only inside the 24h customer-service window — ``apps.whatsapp`` already
    enforces that at send time). Later channels register their own resolution
    here without callers (Inbox UI, Workflow steps) needing to change.
    """

    name = "reply"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation", "body"]}

    def execute(self, context: dict, *, conversation, body: str) -> dict:
        if not body:
            raise ActionError("reply requires a body")

        if conversation.channel == conversation.Channel.WHATSAPP:
            from apps.whatsapp import api as whatsapp_api

            wa_contact = conversation.whatsapp_conversation.contact
            msg = whatsapp_api.send_message(conversation.account, wa_contact, body)
            conversation.whatsapp_conversation.register_outbound(msg.created_at)
            return {"outbound_message_id": msg.id}

        raise ActionError(f"reply not yet implemented for channel {conversation.channel!r}")


class AssignConversationAction(Action):
    """Assign a generic Conversation to a team member, or to nobody (``user=None``).

    The assignee must be a member of the conversation's own account: this is the one place
    that is enforced, so the inbox, workflows and the API cannot hand a customer's
    conversation to someone outside the business.
    """

    name = "assign_conversation"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation", "user"]}

    def execute(self, context: dict, *, conversation, user) -> dict:
        from apps.accounts.models import Membership

        if user is not None and not Membership.objects.filter(
            account_id=conversation.account_id, user=user
        ).exists():
            raise ActionError("That person isn't on your team.")
        conversation.assign(user)
        return {"conversation_id": conversation.id, "assigned_to_id": user.id if user else None}


class AddInternalNoteAction(Action):
    """Attach a staff-only note to a conversation."""

    name = "add_internal_note"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation", "body"], "optional": ["author"]}

    def execute(self, context: dict, *, conversation, body: str, author=None) -> dict:
        from apps.conversations.models import ConversationNote

        if not body:
            raise ActionError("add_internal_note requires a body")
        note = ConversationNote.objects.create(
            account=conversation.account, conversation=conversation,
            author=author, body=body,
        )
        return {"note_id": note.id}


class CreateFollowUpAction(Action):
    """Create a due-date reminder to return to a customer (R2.2)."""

    name = "create_followup"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation", "due_at"], "optional": ["note", "created_by"]}

    def execute(self, context: dict, *, conversation, due_at, note: str = "", created_by=None) -> dict:
        from apps.conversations.models import FollowUp

        followup = FollowUp.objects.create(
            account=conversation.account,
            contact=conversation.contact,
            conversation=conversation,
            due_at=due_at,
            note=note,
            created_by=created_by,
        )
        return {"followup_id": followup.id}


class CompleteFollowUpAction(Action):
    """Mark a follow-up as done."""

    name = "complete_followup"
    scope_kwarg = "followup"

    def input_schema(self) -> dict:
        return {"required": ["followup"]}

    def execute(self, context: dict, *, followup) -> dict:
        followup.mark_done()
        return {"followup_id": followup.id}


register(SendWhatsAppAction())
register(ReplyAction())
register(AssignConversationAction())
register(AddInternalNoteAction())
register(CreateFollowUpAction())
register(CompleteFollowUpAction())

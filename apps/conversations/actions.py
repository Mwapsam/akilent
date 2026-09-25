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


class AutoAssignConversationAction(Action):
    """Give a conversation to a teammate without anyone choosing: the least busy, or a named one.

    "Least busy" is the member with the fewest open conversations already assigned to them
    (ties go to whoever joined first), which spreads work fairly without remembering whose
    turn it was. A conversation that already has an assignee is left alone unless ``force`` is
    set, so a workflow never takes a customer away from the person already looking after them.
    """

    name = "auto_assign_conversation"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation"], "optional": ["email", "force"]}

    def execute(self, context: dict, *, conversation, email: str = "", force: bool = False) -> dict:
        from django.db.models import Count

        from apps.accounts.models import Membership
        from apps.conversations.models import Conversation

        if conversation.assigned_to_id and not force:
            return {"conversation_id": conversation.id, "assigned_to_id": conversation.assigned_to_id,
                    "changed": False}

        members = list(
            Membership.objects.filter(account_id=conversation.account_id, user__is_active=True)
            .select_related("user").order_by("id")
        )
        email = (email or "").strip()
        if email:
            chosen = next((m.user for m in members if (m.user.email or "").lower() == email.lower()), None)
            if chosen is None:
                raise ActionError(f"{email} isn't on your team.")
        else:
            if not members:
                raise ActionError("There is nobody on your team to assign this to.")
            load = dict(
                Conversation.objects.filter(
                    account_id=conversation.account_id, status=Conversation.Status.OPEN,
                    assigned_to__isnull=False,
                ).values_list("assigned_to").annotate(n=Count("id"))
            )
            chosen = min((m.user for m in members), key=lambda u: (load.get(u.id, 0), u.id))
        conversation.assign(chosen)
        return {"conversation_id": conversation.id, "assigned_to_id": chosen.id, "changed": True}


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


class LookupCustomerAction(Action):
    """Read-only: where this conversation's customer stands. Their own records only.

    Open follow-up, interest (lead) status, and their last few orders. No contact details.
    """

    name = "lookup_customer"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation"]}

    def execute(self, context: dict, *, conversation) -> dict:
        from apps.commerce.models import Order
        from apps.conversations.models import FollowUp
        from apps.crm.models import Lead

        account, contact = conversation.account, conversation.contact
        followup = FollowUp.objects.filter(account=account, contact=contact, done_at__isnull=True).order_by("due_at").first()
        lead = Lead.objects.filter(account=account, contact=contact).order_by("-created_at").first()
        orders = Order.objects.filter(account=account, contact=contact).order_by("-created_at")[:3]
        return {
            "first_name": (contact.first_name or "").strip(),
            "customer_since": contact.created_at.date().isoformat() if getattr(contact, "created_at", None) else "",
            "interest": lead.get_status_display() if lead else "not tracked",
            "open_followup": {"due": followup.due_at.date().isoformat(), "note": followup.note} if followup else None,
            "recent_orders": [
                {"date": o.created_at.date().isoformat(), "status": o.get_status_display(),
                 "total": str(o.total), "currency": o.currency}
                for o in orders
            ],
        }


register(SendWhatsAppAction())
register(LookupCustomerAction())
register(ReplyAction())
register(AssignConversationAction())
register(AutoAssignConversationAction())
register(AddInternalNoteAction())
register(CreateFollowUpAction())
register(CompleteFollowUpAction())

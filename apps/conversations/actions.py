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

    def execute(  # type: ignore[override]
        self,
        context: dict,
        *,
        account,
        phone: str,
        template_id: int,
        params: dict | None = None,
        scheduled_at=None,
        conversation=None,
    ) -> dict:
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
                account,
                phone=phone,
                template_id=template_id,
                params=params or {},
                scheduled_at=scheduled_at,
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
        return {"required": ["conversation", "body"], "optional": ["idempotency_key"]}

    def execute(  # type: ignore[override]
        self,
        context: dict,
        *,
        conversation,
        body: str,
        idempotency_key: str | None = None,
    ) -> dict:
        if not body:
            raise ActionError("reply requires a body")

        if conversation.channel == conversation.Channel.WHATSAPP:
            from apps.whatsapp import api as whatsapp_api

            wa_contact = conversation.whatsapp_conversation.contact
            # A caller that may retry (an automatic AI reply) passes a fixed key, so a retry
            # returns the first message instead of sending the customer a second one.
            msg = whatsapp_api.send_message(
                conversation.account, wa_contact, body, idempotency_key=idempotency_key
            )
            conversation.whatsapp_conversation.register_outbound(msg.created_at)
            return {"outbound_message_id": msg.id}

        raise ActionError(
            f"reply not yet implemented for channel {conversation.channel!r}"
        )


class AssignConversationAction(Action):
    """Assign a generic Conversation to a team member, or to nobody (``user=None``).

    The assignee must be a member of the conversation's own account: this is the one place
    that is enforced, so the inbox, workflows and the API cannot hand a customer's
    conversation to someone outside the business.
    """

    name = "assign_conversation"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation", "user"], "optional": ["assigned_by"]}

    def execute(  # type: ignore[override]
        self, context: dict, *, conversation, user, assigned_by=None
    ) -> dict:
        from apps.accounts.models import Membership

        if (
            user is not None
            and not Membership.objects.filter(
                account_id=conversation.account_id, user=user
            ).exists()
        ):
            raise ActionError("That person isn't on your team.")
        actor = f"user:{assigned_by.pk}" if assigned_by else ""
        conversation.assign(user, actor=actor)
        return {
            "conversation_id": conversation.id,
            "assigned_to_id": user.id if user else None,
        }


class AutoAssignConversationAction(Action):
    """Give a conversation to a teammate without anyone choosing: the least busy, or a named one.

    "Least busy" is the member with the fewest open conversations already assigned to them
    (ties go to whoever joined first), which spreads work fairly without remembering whose
    turn it was — the same simple mechanism doubles as B.2's "round-robin" once ``team``
    scopes the candidates to one team's roster, rather than a second algorithm. A conversation
    that already has an assignee is left alone unless ``force`` is set, so a workflow never
    takes a customer away from the person already looking after them.
    """

    name = "auto_assign_conversation"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation"], "optional": ["email", "force", "team"]}

    def execute(  # type: ignore[override]
        self,
        context: dict,
        *,
        conversation,
        email: str = "",
        force: bool = False,
        team=None,
    ) -> dict:
        from django.db.models import Count

        from apps.accounts.models import Membership
        from apps.conversations.models import Conversation

        if conversation.assigned_to_id and not force:
            return {
                "conversation_id": conversation.id,
                "assigned_to_id": conversation.assigned_to_id,
                "changed": False,
            }

        members_qs = Membership.objects.filter(
            account_id=conversation.account_id, user__is_active=True
        )
        if team is not None:
            members_qs = members_qs.filter(user__teams=team)
        members = list(members_qs.select_related("user").order_by("id"))
        email = (email or "").strip()
        if email:
            chosen = next(
                (
                    m.user
                    for m in members
                    if (m.user.email or "").lower() == email.lower()
                ),
                None,
            )
            if chosen is None:
                raise ActionError(f"{email} isn't on your team.")
        else:
            if not members:
                raise ActionError("There is nobody on your team to assign this to.")
            load = dict(
                Conversation.objects.filter(
                    account_id=conversation.account_id,
                    status=Conversation.Status.OPEN,
                    assigned_to__isnull=False,
                )
                .values_list("assigned_to")
                .annotate(n=Count("id"))
            )
            chosen = min(
                (m.user for m in members), key=lambda u: (load.get(u.id, 0), u.id)
            )
        conversation.assign(chosen, actor="automation:auto_assign_conversation")
        return {
            "conversation_id": conversation.id,
            "assigned_to_id": chosen.id,
            "changed": True,
        }


class RouteConversationAction(Action):
    """Give a brand-new conversation to a team, then to an agent on that team —
    the entry point Phase B.2 wires up on conversation creation.

    Never raises for "no rule matched" or "team has nobody active": both are
    valid, expected outcomes (team/unassigned, or fully unassigned) per the
    fallback the plan calls out — a conversation must never disappear because
    routing couldn't fully resolve it. The Unassigned inbox tab (optionally
    narrowed to a team) is where it lands instead.
    """

    name = "route_conversation"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation"]}

    def execute(self, context: dict, *, conversation) -> dict:  # type: ignore[override]
        from apps.conversations.routing import match_team

        team = match_team(conversation)
        if team is None:
            return {
                "conversation_id": conversation.id,
                "team_id": None,
                "assigned_to_id": None,
            }

        conversation.set_team(team, actor="automation:route_conversation")
        try:
            result = run_action(
                "auto_assign_conversation",
                context,
                conversation=conversation,
                team=team,
            )
            assigned_to_id = result.get("assigned_to_id")
        except ActionError:
            # Team has nobody active right now — team/unassigned, not an error.
            assigned_to_id = None
        return {
            "conversation_id": conversation.id,
            "team_id": team.id,
            "assigned_to_id": assigned_to_id,
        }


class StartConversationFormAction(Action):
    """Start a ``ConversationForm`` on a conversation: sends its first question.

    Callable from a Workflow's generic ``action`` step (no workflow_engine change
    needed — see its own docstring) or from the inbox directly. Refuses to start a
    second form while one is already in progress on this conversation, rather than
    silently abandoning the first.
    """

    name = "start_conversation_form"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation", "form"]}

    def execute(self, context: dict, *, conversation, form) -> dict:  # type: ignore[override]
        from apps.conversations import forms as conversation_forms

        if conversation_forms.active_response_for(conversation) is not None:
            raise ActionError("A form is already in progress on this conversation.")
        try:
            response = conversation_forms.start_form(conversation, form)
        except conversation_forms.FormError as exc:
            raise ActionError(str(exc)) from exc
        return {"conversation_id": conversation.id, "response_id": response.id}


class AddInternalNoteAction(Action):
    """Attach a staff-only note to a conversation."""

    name = "add_internal_note"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation", "body"], "optional": ["author"]}

    def execute(self, context: dict, *, conversation, body: str, author=None) -> dict:  # type: ignore[override]
        from apps.conversations.models import ConversationNote

        if not body:
            raise ActionError("add_internal_note requires a body")
        note = ConversationNote.objects.create(
            account=conversation.account,
            conversation=conversation,
            author=author,
            body=body,
        )
        return {"note_id": note.id}


class CreateFollowUpAction(Action):
    """Create a due-date reminder to return to a customer (R2.2)."""

    name = "create_followup"
    scope_kwarg = "conversation"

    def input_schema(self) -> dict:
        return {
            "required": ["conversation", "due_at"],
            "optional": ["note", "created_by"],
        }

    def execute(  # type: ignore[override]
        self, context: dict, *, conversation, due_at, note: str = "", created_by=None
    ) -> dict:
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

    def execute(self, context: dict, *, followup) -> dict:  # type: ignore[override]
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

    def execute(self, context: dict, *, conversation) -> dict:  # type: ignore[override]
        from apps.commerce.models import Order
        from apps.conversations.models import FollowUp
        from apps.crm.models import Lead

        account, contact = conversation.account, conversation.contact
        followup = (
            FollowUp.objects.filter(
                account=account, contact=contact, done_at__isnull=True
            )
            .order_by("due_at")
            .first()
        )
        lead = (
            Lead.objects.filter(account=account, contact=contact)
            .order_by("-created_at")
            .first()
        )
        orders = Order.objects.filter(account=account, contact=contact).order_by(
            "-created_at"
        )[:3]
        return {
            "first_name": (contact.first_name or "").strip(),
            "customer_since": contact.created_at.date().isoformat()
            if getattr(contact, "created_at", None)
            else "",
            "interest": lead.get_status_display() if lead else "not tracked",
            "open_followup": {
                "due": followup.due_at.date().isoformat(),
                "note": followup.note,
            }
            if followup
            else None,
            "recent_orders": [
                {
                    "date": o.created_at.date().isoformat(),
                    "status": o.get_status_display(),
                    "total": str(o.total),
                    "currency": o.currency,
                }
                for o in orders
            ],
        }


register(SendWhatsAppAction())
register(LookupCustomerAction())
register(ReplyAction())
register(AssignConversationAction())
register(AutoAssignConversationAction())
register(RouteConversationAction())
register(AddInternalNoteAction())
register(StartConversationFormAction())
register(CreateFollowUpAction())
register(CompleteFollowUpAction())

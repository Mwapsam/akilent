"""Phase 1 Action Registry.

The single execution surface Workflows, AI, the API, and webhooks share.
Phase 1 establishes the *contract* and ships three actions
(``send_whatsapp``, ``assign_conversation``, ``add_internal_note``); Phase 4
expands the registry with the full CRM/Commerce/Booking action set. The
boundary matters more than the breadth at this stage.

Every action implements the same shape (name, version, input schema,
permission check, execute) so callers never need to know which module owns
the underlying state.
"""
from __future__ import annotations

import abc
import logging

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """Raised when an action's input is invalid or it cannot be executed."""


class Action(abc.ABC):
    """Base contract every registry action implements."""

    name: str
    version: int = 1

    def input_schema(self) -> dict:
        """JSON-schema-like description of accepted kwargs. Advisory in Phase 1."""
        return {}

    def has_permission(self, context: dict) -> bool:
        """Whether the caller (given in ``context``) may run this action.

        Phase 1 default is permissive (any authenticated account context);
        modules that need finer-grained checks override this.
        """
        return True

    @abc.abstractmethod
    def execute(self, context: dict, **kwargs) -> dict:
        """Perform the action. Must return a JSON-serializable result dict."""
        raise NotImplementedError


_REGISTRY: dict[str, Action] = {}


def register(action: Action) -> Action:
    if action.name in _REGISTRY:
        raise ActionError(f"action {action.name!r} is already registered")
    _REGISTRY[action.name] = action
    return action


def get_action(name: str) -> Action:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ActionError(f"no action registered as {name!r}") from None


def available_actions() -> list[str]:
    return sorted(_REGISTRY)


def run_action(name: str, context: dict, **kwargs) -> dict:
    """Look up, permission-check, and execute a registered action.

    This is the one call site every caller (Workflow steps, future AI,
    API endpoints, webhooks) should go through — never call an action's
    ``execute`` directly.
    """
    action = get_action(name)
    if not action.has_permission(context):
        raise ActionError(f"action {name!r} not permitted for this context")
    return action.execute(context, **kwargs)


class SendWhatsAppAction(Action):
    """Send a WhatsApp template message to a Contact.

    Reuses ``apps.automation.workflows.send_whatsapp_message`` — the same
    function the Workflow engine's ``send_whatsapp`` step and the legacy
    AutomationRule action call — so there remains exactly one place that
    talks to WhatsAppContact/MessageTemplate/OutboundMessage.
    """

    name = "send_whatsapp"

    def input_schema(self) -> dict:
        return {
            "required": ["account", "phone", "template_id"],
            "optional": ["params", "scheduled_at"],
        }

    def execute(self, context: dict, *, account, phone: str, template_id: int,
                params: dict | None = None, scheduled_at=None) -> dict:
        from apps.automation.workflows import send_whatsapp_message

        if not phone:
            raise ActionError("send_whatsapp requires a phone number")
        msg = send_whatsapp_message(
            account, phone=phone, template_id=template_id,
            params=params or {}, scheduled_at=scheduled_at,
        )
        return {"outbound_message_id": msg.id}


class AssignConversationAction(Action):
    """Assign a generic Conversation to a team member."""

    name = "assign_conversation"

    def input_schema(self) -> dict:
        return {"required": ["conversation", "user"]}

    def execute(self, context: dict, *, conversation, user) -> dict:
        conversation.assign(user)
        return {"conversation_id": conversation.id, "assigned_to_id": user.id}


class AddInternalNoteAction(Action):
    """Attach a staff-only note to a conversation."""

    name = "add_internal_note"

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


register(SendWhatsAppAction())
register(AssignConversationAction())
register(AddInternalNoteAction())

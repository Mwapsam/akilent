import logging

from apps.automation.models import AutomationRule
from apps.whatsapp.models import OutboundMessage

logger = logging.getLogger(__name__)


def execute_rule(rule: AutomationRule, context: dict) -> None:
    """Dispatch the rule's action to the appropriate handler."""
    action = rule.action
    action_type = action.get("type")

    handler = _ACTION_HANDLERS.get(action_type)
    if handler is None:
        logger.warning(
            "execute_rule: unknown action type '%s' for rule pk=%s", action_type, rule.pk
        )
        return

    logger.info("execute_rule: running '%s' for rule pk=%s", action_type, rule.pk)
    handler(rule, action, context)


def _resolve_or_provision_contact(account, phone: str, *, auto_create: bool = False):
    """Resolve the ``WhatsAppContact`` for ``phone``, or provision one if allowed.

    By default (``auto_create=False``) a missing contact/opt-in record is a hard
    failure with an actionable message — safer, since WhatsApp sends require
    consent and we don't want to invent contacts silently. When ``auto_create``
    is explicitly set (e.g. a workflow step opts in via ``auto_create_contact``),
    a new record is created in the existing ``UNKNOWN`` opt-in state — never
    ``OPTED_IN`` — so the real consent decision is still made by
    ``apps.whatsapp``'s send-authorization policy, not by this helper.
    """
    from apps.whatsapp.models import WhatsAppContact

    contact = WhatsAppContact.objects.filter(account=account, phone_number=phone).first()
    if contact is not None:
        return contact
    if not auto_create:
        raise ValueError(
            f"no WhatsApp contact/opt-in record for {phone!r} on account {account.pk}; "
            "add one under WhatsApp Contacts or enable auto_create_contact for this step"
        )
    return WhatsAppContact.objects.create(account=account, phone_number=phone)


def send_whatsapp_message(
    account, *, phone: str, template_id, params: dict | None = None, scheduled_at=None,
    auto_create_contact: bool = False,
) -> OutboundMessage:
    """Queue a WhatsApp template message to ``phone`` on ``account``.

    Shared sending path: both the legacy AutomationRule action handler and the
    Workflow engine's ``send_whatsapp`` step call this, so there is exactly one
    place that talks to WhatsAppContact/MessageTemplate/OutboundMessage.

    ``scheduled_at`` (optional) sets when OutboundMessage.scheduled_at becomes
    due — it's already the field ``apps.whatsapp.tasks.drain_outbound_queue``
    polls, so this just exposes it instead of always defaulting to "now".

    Raises ``ValueError`` (with an actionable message) if there's no matching
    ``WhatsAppContact`` and ``auto_create_contact`` is not set, or
    ``MessageTemplate.DoesNotExist`` if the template can't be resolved for this
    account.
    """
    from apps.whatsapp.models import MessageTemplate

    contact = _resolve_or_provision_contact(account, phone, auto_create=auto_create_contact)
    template = MessageTemplate.objects.get(pk=template_id, account=account)

    kwargs = {}
    if scheduled_at is not None:
        kwargs["scheduled_at"] = scheduled_at

    return OutboundMessage.objects.create(
        account=account,
        contact=contact,
        template=template,
        payload={
            "type": "template",
            "template_name": template.whatsapp_template_name,
            "language": template.language_code,
            "params": params or {},
        },
        **kwargs,
    )


def _send_whatsapp_message(rule: AutomationRule, action: dict, context: dict) -> None:
    """
    Action payload example:
        {"type": "send_whatsapp_message", "template_id": 42, "params": {...}}
    """
    from apps.whatsapp.models import MessageTemplate

    phone = context.get("phone_number")
    template_id = action.get("template_id")
    if not phone or not template_id:
        logger.warning("_send_whatsapp_message: missing phone or template_id in rule pk=%s", rule.pk)
        return

    try:
        send_whatsapp_message(
            rule.account, phone=phone, template_id=template_id, params=action.get("params", {})
        )
    except (ValueError, MessageTemplate.DoesNotExist) as exc:
        logger.error("_send_whatsapp_message: %s", exc)
        return


_ACTION_HANDLERS = {
    "send_whatsapp_message": _send_whatsapp_message,
}

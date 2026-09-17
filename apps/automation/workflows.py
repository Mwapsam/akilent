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


def send_whatsapp_message(account, *, phone: str, template_id, params: dict | None = None) -> OutboundMessage:
    """Queue a WhatsApp template message to ``phone`` on ``account``.

    Shared sending path: both the legacy AutomationRule action handler and the
    Workflow engine's ``send_whatsapp`` step call this, so there is exactly one
    place that talks to WhatsAppContact/MessageTemplate/OutboundMessage.

    Raises ``WhatsAppContact.DoesNotExist`` / ``MessageTemplate.DoesNotExist``
    if the contact or template can't be resolved for this account.
    """
    from apps.whatsapp.models import MessageTemplate, WhatsAppContact

    contact = WhatsAppContact.objects.get(account=account, phone_number=phone)
    template = MessageTemplate.objects.get(pk=template_id, account=account)

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
    )


def _send_whatsapp_message(rule: AutomationRule, action: dict, context: dict) -> None:
    """
    Action payload example:
        {"type": "send_whatsapp_message", "template_id": 42, "params": {...}}
    """
    from apps.whatsapp.models import MessageTemplate, WhatsAppContact

    phone = context.get("phone_number")
    template_id = action.get("template_id")
    if not phone or not template_id:
        logger.warning("_send_whatsapp_message: missing phone or template_id in rule pk=%s", rule.pk)
        return

    try:
        send_whatsapp_message(
            rule.account, phone=phone, template_id=template_id, params=action.get("params", {})
        )
    except (WhatsAppContact.DoesNotExist, MessageTemplate.DoesNotExist) as exc:
        logger.error("_send_whatsapp_message: %s", exc)
        return


_ACTION_HANDLERS = {
    "send_whatsapp_message": _send_whatsapp_message,
}

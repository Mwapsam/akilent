import logging

from apps.core.events import MessageReceived, MessageStatusChanged
from apps.whatsapp import api as whatsapp_api

logger = logging.getLogger(__name__)


def on_message_received(event: MessageReceived, **kwargs) -> None:
    from apps.automation.tasks import evaluate_rules_for_message
    from apps.automation.models import AutomationRule
    from apps.accounts import api as accounts_api

    # Get the contact details for context
    try:
        account = accounts_api.get_account(event.account_id)
        contact = whatsapp_api.get_contact(account, event.contact_id)
    except Exception:
        logger.warning(
            "on_message_received: contact %s not found for account %s",
            event.contact_id,
            event.account_id,
        )
        return

    context = {
        "phone_number": contact.phone_number,
        "message_type": event.message_type,
        "message_contains": event.body,
    }

    # Dispatch rule evaluation to Celery asynchronously (automation queue)
    evaluate_rules_for_message.delay(
        event.account_id,
        AutomationRule.TriggerEvent.MESSAGE_RECEIVED,
        context,
    )

    # Modern Workflow engine: enroll on a "whatsapp.received" trigger. Kept in
    # its own try/except — a failure here must never affect the legacy
    # AutomationRule dispatch above, which has already been queued by this point.
    try:
        _enroll_workflows_for_reply(event, contact)
    except Exception:
        logger.exception(
            "on_message_received: _enroll_workflows_for_reply failed for account=%s contact=%s",
            event.account_id, event.contact_id,
        )


def _enroll_workflows_for_reply(event: MessageReceived, wa_contact) -> None:
    """Enroll published Workflows with trigger ``whatsapp.received``.

    Resolves the ``apps.contacts.Contact`` linked to this WhatsApp identity —
    directly via ``wa_contact.contact`` if already linked, otherwise by a
    normalized-phone lookup (backfilling the link for next time). If no
    ``Contact`` can be resolved, this is a no-op (logged) rather than
    fabricating one — see the plan's "explicitly out of scope" note on why
    Contact creation isn't attempted here (email is required on Contact today).
    """
    from apps.automation.workflow_engine import enroll_for_trigger
    from apps.contacts.models import Contact
    from apps.whatsapp.models.contact import normalize_phone

    contact = wa_contact.contact
    if contact is None:
        normalized = normalize_phone(wa_contact.phone_number)
        contact = Contact.objects.filter(account_id=event.account_id, phone=normalized).first()
        if contact is not None:
            wa_contact.contact = contact
            wa_contact.save(update_fields=["contact"])

    if contact is None:
        logger.info(
            "_enroll_workflows_for_reply: no linked Contact for WhatsAppContact %s (account=%s)",
            wa_contact.pk, event.account_id,
        )
        return

    enroll_for_trigger(event.account_id, "whatsapp.received", contact, context={
        "message": {
            "body": event.body,
            "type": event.message_type,
            "message_id": event.message_id,
        },
    })


def on_message_sent(message_log) -> None:
    from apps.automation.models import AutomationRule

    context = {
        "phone_number": message_log.contact.phone_number,
        "message_type": message_log.message_type,
    }
    _dispatch(message_log.account_id, AutomationRule.TriggerEvent.MESSAGE_SENT, context)


def on_lead_created(account_id: int, lead_id: str, fields: dict) -> None:
    pass


def on_deal_stage_changed(account_id: int, deal_id: str, stage_id: str) -> None:
    pass


def _dispatch(account_id: int, event: str, context: dict) -> None:
    from apps.automation.rules import evaluate_conditions, get_matching_rules
    from apps.automation.workflows import execute_rule

    for rule in get_matching_rules(account_id, event):
        if evaluate_conditions(rule, context):
            try:
                execute_rule(rule, context)
            except Exception:
                logger.exception("_dispatch: error executing rule pk=%s", rule.pk)

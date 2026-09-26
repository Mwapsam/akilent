"""The Operator Console's audit trail: who changed what for which business.

Every console POST calls ``audit``; "View as" start and stop are recorded too. Failures to record
are logged, never raised, so an audit hiccup can't block fixing a customer's problem.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Plain-language labels for the audit page.
LABELS = {
    "account.suspend": "Suspended the business",
    "account.activate": "Reactivated the business",
    "account.close": "Closed the account",
    "subscription.set": "Changed plan or status",
    "subscription.extend_trial": "Extended the trial",
    "module.toggle": "Turned a module on or off",
    "payment.approve": "Approved a payment",
    "payment.reject": "Rejected a payment",
    "plan.create": "Created a plan",
    "plan.edit": "Edited a plan",
    "plan.toggle": "Showed or hid a plan",
    "plan.delete": "Deleted a plan",
    "plan.sync_flutterwave": "Synced a plan to Flutterwave",
    "payment_method.toggle": "Turned a payment method on or off",
    "payment_method.edit": "Edited payment instructions",
    "settings.site": "Changed site settings",
    "settings.mail": "Changed email settings",
    "configuration.create": "Added a configuration",
    "configuration.edit": "Edited a configuration",
    "configuration.delete": "Deleted a configuration",
    "operator.grant": "Made someone an operator",
    "operator.revoke": "Removed an operator",
    "view_as.start": "Started viewing as the business",
    "view_as.stop": "Stopped viewing as the business",
    "whatsapp.retry_registration": "Retried WhatsApp number registration",
    "whatsapp.sync_templates": "Synced WhatsApp templates",
    "whatsapp.retry_send": "Retried a failed WhatsApp message",
    "whatsapp.resend_webhook": "Reprocessed a WhatsApp webhook",
    "email.reset_reputation": "Reset the email sending halt",
    "email.reverify_domain": "Re-checked a sending domain",
    "workflow.pause": "Paused an automation",
    "ai.autopilot_off": "Turned AI autopilot off",
    "ai.off": "Turned AI off",
    "invitation.resend": "Resent an invitation",
    "invitation.revoke": "Revoked an invitation",
    "team.change_owner": "Changed the account owner",
    "api_key.revoke": "Revoked an API key",
    "job.cancel": "Cancelled a scheduled job",
    "data.export_contacts": "Exported contacts",
    "data.delete_customer": "Deleted a customer's data",
}


def audit(request, action: str, account=None, target: str = "", **detail) -> None:
    from apps.core.models import AdminAction

    try:
        AdminAction.objects.create(
            actor=request.user if request.user.is_authenticated else None,
            account=account, action=action, target=str(target)[:200], detail=detail,
        )
    except Exception:
        logger.exception("audit: couldn't record %s", action)


def label(action: str) -> str:
    return LABELS.get(action, action.replace("_", " ").replace(".", ": "))

"""Audit trails for the Operator Console and platform-wide business actions.

``audit()`` records Operator Console actions (operators only).
``platform_record()`` records sensitive business-user actions visible to the
business (team changes, auth events, campaign sends, AI recommendations).

Every console POST calls ``audit``; "View as" start and stop are recorded too. Failures to record
are logged, never raised, so an audit hiccup can't block fixing a customer's problem.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Plain-language labels for the audit page.
LABELS = {
    "account.suspend": "Suspended the business",
    "account.activate": "Reactivated the business",
    "account.close": "Closed the account",
    "subscription.set": "Changed plan or status",
    "subscription.extend_trial": "Extended the trial",
    "module.toggle": "Turned a module on or off",
    "business.feature.grant": "Granted a feature to the business",
    "business.feature.remove": "Removed a feature from the business",
    "business.feature.reset": "Put a feature back to what the plan includes",
    "business.limit.override": "Gave the business its own limit",
    "business.limit.reset": "Put a limit back to what the plan allows",
    "plan.limit.change": "Changed a plan's limit",
    "plan.loss_acknowledged": "Accepted that a plan can lose money",
    "costs.save": "Changed unit costs",
    "plan.feature.add": "Added a feature to a plan",
    "plan.feature.remove": "Removed a feature from a plan",
    "coming_soon.save": "Saved a coming-soon feature",
    "coming_soon.delete": "Removed a coming-soon feature",
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
            account=account,
            action=action,
            target=str(target)[:200],
            detail=detail,
        )
    except Exception:
        logger.exception("audit: couldn't record %s", action)


def label(action: str) -> str:
    return LABELS.get(action, action.replace("_", " ").replace(".", ": "))


def platform_record(
    *,
    action: str,
    account=None,
    actor=None,
    resource_type: str = "",
    resource_id: str = "",
    ip_address: str | None = None,
    success: bool = True,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write one PlatformAuditLog entry for a business-user action.

    Args:
        action:        Dot-namespaced string, e.g. "team.invite", "auth.login_failed".
        account:       The Account context (None for pre-auth events like login failure).
        actor:         The authenticated User who triggered the action (None for system).
        resource_type: "user", "campaign", "invitation", etc.
        resource_id:   Identifier of the resource (email, public_id, etc.).
        ip_address:    Client IP; include for auth events.
        success:       Whether the action succeeded.
        metadata:      Extra key/value pairs.

    Best-effort: a DB write failure logs an error but never raises.
    """
    from apps.core.models import PlatformAuditLog

    try:
        PlatformAuditLog.objects.create(
            account=account,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=(resource_id or "")[:255],
            ip_address=ip_address,
            success=success,
            metadata=metadata or {},
        )
    except Exception:
        logger.exception("platform_record: failed to record action=%s", action)

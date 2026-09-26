"""Everything the Operator Console shows and does for one business (the business page tabs).

Reads and small fixes across apps, all scoped to one ``account``. Other apps' models are imported
inside functions (module boundary rule); where an app already offers a function (registration,
reputation reset, job cancel), it's used rather than re-implemented. Views record every action in
the audit log; nothing here does.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

TABS = [
    ("overview", "Overview"), ("billing", "Billing"), ("whatsapp", "WhatsApp"), ("email", "Email"),
    ("automations", "Automations"), ("ai", "AI"), ("team", "Team"), ("api", "API & jobs"),
    ("data", "Data"), ("activity", "Activity"),
]


class ConsoleError(Exception):
    """An action couldn't be done; the message is shown to the operator as is."""


# ---- tab data ---------------------------------------------------------------------------------

def overview(account) -> dict:
    from apps.conversations import api as conversations_api
    from apps.core.console.attention import reasons
    from apps.whatsapp import api as whatsapp_api

    now = timezone.now()
    return {
        "reasons": reasons(account, now),
        "owner": account.owner,
        "connected_at": whatsapp_api.connected_since(account),
        "activity": conversations_api.activity(account, since=now - timedelta(days=7)),
        "starting_point": conversations_api.starting_point(account, now),
    }


def billing(account) -> dict:
    from apps.billing import api as billing_api
    from apps.billing.models import ManualPaymentRequest, ModuleSubscription, Plan, Subscription, UsageSummary

    rows = {m.module: m for m in ModuleSubscription.objects.filter(account=account)}
    modules = [{"key": key, "label": label, "enabled": rows[key].enabled if key in rows else True,
                "explicit": key in rows, "billing_status": rows[key].get_billing_status_display() if key in rows else ""}
               for key, label in ModuleSubscription.MODULE_CHOICES]
    return {
        "subscription": billing_api.get_subscription(account),
        "emails_used": UsageSummary.get_current_email_usage(account),
        "conversations_used": UsageSummary.get_current_usage(account),
        "modules": modules,
        "payments": ManualPaymentRequest.objects.filter(account=account).select_related("plan").order_by("-created_at")[:10],
        "plans": Plan.objects.order_by("price_monthly"),
        "statuses": Subscription.STATUS_CHOICES,
    }


def whatsapp(account) -> dict:
    from apps.whatsapp.friendly_errors import friendly_send_error
    from apps.whatsapp.health import number_health
    from apps.whatsapp.models import MessageTemplate, OutboundMessage, WebhookEventLog
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

    numbers = list(WhatsAppBusinessNumber.objects.filter(account=account).order_by("-is_active", "created_at"))
    failed = (OutboundMessage.objects.filter(account=account, status=OutboundMessage.Status.FAILED,
                                             updated_at__gte=timezone.now() - timedelta(days=7))
              .select_related("contact").order_by("-updated_at")[:25])
    ids = [n.phone_number_id for n in numbers]
    webhooks = (WebhookEventLog.objects.filter(
        processed=False, payload__entry__0__changes__0__value__metadata__phone_number_id__in=ids)
        .order_by("-created_at")[:25]) if ids else []
    return {
        "numbers": [{"number": n, "status": n.get_registration_status_display(),
                     "setup": n.setup_status, "health": number_health(n)} for n in numbers],
        "templates": MessageTemplate.objects.filter(account=account).order_by("approval_status", "name"),
        "failed": [{"message": m, "reason": friendly_send_error(m.error_code or (m.last_error or "").split(":")[0]),
                    "retryable": not (m.payload or {}).get("kind")} for m in failed],
        "webhooks": webhooks,
    }


def email(account) -> dict:
    from apps.email.models import EmailDomain, EmailMessage, SendReputation, SuppressionListEntry

    return {
        "domains": EmailDomain.objects.filter(account=account).order_by("domain"),
        "reputation": SendReputation.objects.filter(account=account).first(),
        "suppressed": SuppressionListEntry.objects.filter(account=account).count(),
        "failures": EmailMessage.objects.filter(account=account, status__in=["failed", "bounced", "complained"])
        .order_by("-created_at")[:15],
    }


def automations(account) -> dict:
    from apps.automation import api as automation_api
    from apps.automation.models import Workflow, WorkflowRun

    now = timezone.now()
    failed = (WorkflowRun.objects.filter(workflow__account=account, status=WorkflowRun.Status.FAILED)
              .select_related("workflow").prefetch_related("step_runs").order_by("-started_at")[:15])
    return {
        "workflows": Workflow.objects.filter(account=account).order_by("status", "name"),
        "failed_runs": [{"run": r, "error": next((str((s.result or {}).get("error", ""))
                                                   for s in reversed(list(r.step_runs.all())) if s.status != "ok"), "")}
                        for r in failed],
        "overdue": WorkflowRun.objects.filter(workflow__account=account, status=WorkflowRun.Status.WAITING,
                                              next_due_at__lt=now - timedelta(minutes=15)).count(),
        "adoption": automation_api.adoption(account),
    }


def ai(account) -> dict:
    from django.conf import settings
    from django.core.cache import cache

    from apps.ai import api as ai_api
    from apps.ai.models import AISettings

    return {
        "settings": AISettings.objects.filter(account=account).first(),
        "unavailable": ai_api.unavailable_reason(account),
        "calls_today": cache.get(f"ai-calls:{account.pk}:{timezone.now().date().isoformat()}", 0),
        "daily_limit": getattr(settings, "AI_DAILY_CALL_LIMIT", 500),
        "usage": ai_api.usage_summary(account, since=timezone.now() - timedelta(days=7)),
        "autonomy_site_on": getattr(settings, "AI_AUTONOMY_ENABLED", False),
    }


def team(account) -> dict:
    from apps.accounts.models import Invitation

    return {
        "members": account.memberships.select_related("user").order_by("role", "user__email"),
        "invitations": Invitation.objects.filter(account=account, accepted_at__isnull=True).order_by("-created_at"),
    }


def api_and_jobs(account) -> dict:
    from apps.email.models import EmailApiKey
    from apps.scheduler.models import ScheduledJob

    now = timezone.now()
    return {
        "keys": EmailApiKey.objects.filter(account=account).order_by("-is_active", "-created_at"),
        "failed_jobs": ScheduledJob.objects.filter(account=account, status=ScheduledJob.Status.FAILED).order_by("-fire_at")[:15],
        "overdue_jobs": ScheduledJob.objects.filter(account=account, status=ScheduledJob.Status.SCHEDULED,
                                                    fire_at__lt=now - timedelta(minutes=10)).order_by("fire_at")[:15],
        "upcoming_jobs": ScheduledJob.objects.filter(account=account, status=ScheduledJob.Status.SCHEDULED,
                                                     fire_at__gte=now).order_by("fire_at")[:15],
    }


def data(account) -> dict:
    from apps.contacts.models import Contact
    from apps.conversations.models import Conversation, Message

    return {
        "contacts": Contact.objects.filter(account=account).count(),
        "conversations": Conversation.objects.filter(account=account).count(),
        "messages": Message.objects.filter(account=account).count(),
    }


def activity(account) -> dict:
    from apps.core.audit import label
    from apps.core.models import AdminAction

    rows = AdminAction.objects.filter(account=account).select_related("actor")[:50]
    return {"actions": [{"row": r, "label": label(r.action)} for r in rows]}


TAB_DATA = {
    "overview": overview, "billing": billing, "whatsapp": whatsapp, "email": email,
    "automations": automations, "ai": ai, "team": team, "api": api_and_jobs, "data": data,
    "activity": activity,
}


# ---- actions ----------------------------------------------------------------------------------

def set_subscription(account, plan_id, status) -> str:
    from apps.billing.models import Plan, Subscription

    plan = Plan.objects.filter(pk=plan_id).first() if plan_id else None
    if plan is None or status not in dict(Subscription.STATUS_CHOICES):
        raise ConsoleError("Pick a valid plan and status.")
    now = timezone.now()
    sub, created = Subscription.objects.get_or_create(
        account=account, defaults={"plan": plan, "status": status, "current_period_start": now,
                                   "current_period_end": now + timedelta(days=30)})
    if not created:
        sub.plan, sub.status = plan, status
        if status == Subscription.CANCELLED:
            sub.cancelled_at = sub.cancelled_at or now
        elif status in (Subscription.ACTIVE, Subscription.TRIALING):
            sub.cancelled_at = None
            if not sub.current_period_end or sub.current_period_end < now:
                sub.current_period_end = now + timedelta(days=30)
        sub.save()
    return f"{plan.name} ({sub.get_status_display()})"


def extend_trial(account, days: int) -> str:
    from apps.billing.models import Subscription

    sub = Subscription.objects.filter(account=account).first()
    if sub is None:
        raise ConsoleError("This business has no subscription yet. Set a plan first.")
    if not 1 <= days <= 90:
        raise ConsoleError("Extend by 1 to 90 days.")
    base = max(sub.trial_ends_at or timezone.now(), timezone.now())
    sub.trial_ends_at = base + timedelta(days=days)
    sub.status = Subscription.TRIALING
    sub.save(update_fields=["trial_ends_at", "status"])
    return f"Trial now ends {timezone.localtime(sub.trial_ends_at):%d %b %Y}"


def toggle_module(account, module: str) -> bool:
    from apps.billing import api as billing_api
    from apps.billing.models import ModuleSubscription

    if module not in dict(ModuleSubscription.MODULE_CHOICES):
        raise ConsoleError("Unknown module.")
    enabled = not billing_api.module_enabled(account, module)
    billing_api.set_module_enabled(account, module, enabled)
    return enabled


def retry_registration(account, number_pk) -> str:
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
    from apps.whatsapp.registration import register_number

    number = WhatsAppBusinessNumber.objects.filter(account=account, pk=number_pk).first()
    if number is None:
        raise ConsoleError("That number doesn't belong to this business.")
    result = register_number(number)
    number.refresh_from_db()
    if number.registration_status != WhatsAppBusinessNumber.RegistrationStatus.REGISTERED:
        raise ConsoleError(f"Registration still failing: {number.registration_error or getattr(result, 'error', '')}")
    return number.display_number or number.phone_number_id


def sync_templates(account) -> dict:
    from apps.whatsapp import api as whatsapp_api

    return whatsapp_api.import_templates(account)


def retry_send(account, message_pk) -> None:
    from apps.whatsapp.models import MessageLog, OutboundMessage

    msg = OutboundMessage.objects.filter(account=account, pk=message_pk, status=OutboundMessage.Status.FAILED).first()
    if msg is None:
        raise ConsoleError("That failed message isn't there any more.")
    if (msg.payload or {}).get("kind"):
        raise ConsoleError("One-time codes can't be resent: the code isn't kept. The customer can ask for a new one.")
    msg.status, msg.attempts, msg.next_attempt_at = OutboundMessage.Status.QUEUED, 0, None
    msg.error_code, msg.last_error = "", ""
    msg.save(update_fields=["status", "attempts", "next_attempt_at", "error_code", "last_error"])
    if msg.message_log_id:
        MessageLog.objects.filter(pk=msg.message_log_id).update(status=MessageLog.Status.QUEUED)
    from apps.whatsapp.tasks import drain_outbound_queue

    drain_outbound_queue.delay()


def resend_webhook(account, event_pk) -> None:
    from apps.whatsapp.models import WebhookEventLog
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
    from apps.whatsapp.tasks import process_whatsapp_event

    ids = list(WhatsAppBusinessNumber.objects.filter(account=account).values_list("phone_number_id", flat=True))
    event = WebhookEventLog.objects.filter(
        pk=event_pk, payload__entry__0__changes__0__value__metadata__phone_number_id__in=ids).first()
    if event is None:
        raise ConsoleError("That webhook doesn't belong to this business.")
    process_whatsapp_event.delay(event.pk)


def reset_reputation(account) -> None:
    from apps.email.services.reputation import reset

    reset(account)


def reverify_domain(account, domain_pk) -> str:
    from apps.email.models import EmailDomain

    domain = EmailDomain.objects.filter(account=account, pk=domain_pk).first()
    if domain is None:
        raise ConsoleError("That domain doesn't belong to this business.")
    from apps.email.verification import refresh_domain

    refresh_domain(domain)
    domain.refresh_from_db()
    return domain.get_status_display()


def pause_workflow(account, workflow_pk) -> str:
    from apps.automation.models import Workflow

    wf = Workflow.objects.filter(account=account, pk=workflow_pk).first()
    if wf is None:
        raise ConsoleError("That automation doesn't belong to this business.")
    if wf.status == Workflow.Status.PUBLISHED:
        wf.status = Workflow.Status.ARCHIVED
        wf.save(update_fields=["status", "updated_at"])
    return wf.name


def ai_off(account, *, autopilot_only: bool) -> None:
    from apps.ai.models import AISettings

    rows = AISettings.objects.filter(account=account)
    rows.update(reply_mode="suggest") if autopilot_only else rows.update(enabled=False, reply_mode="suggest")


def resend_invitation(request, account, invite_pk) -> str:
    from apps.accounts.models import Invitation
    from apps.accounts.settings_views import _send_invitation_email

    invite = Invitation.objects.filter(account=account, pk=invite_pk, accepted_at__isnull=True).first()
    if invite is None:
        raise ConsoleError("That invitation isn't pending.")
    invite.created_at = timezone.now()  # a fresh 7 days
    invite.save(update_fields=["created_at"])
    _send_invitation_email(request, invite)
    return invite.email


def revoke_invitation(account, invite_pk) -> str:
    from apps.accounts.models import Invitation

    invite = Invitation.objects.filter(account=account, pk=invite_pk, accepted_at__isnull=True).first()
    if invite is None:
        raise ConsoleError("That invitation isn't pending.")
    email = invite.email
    invite.delete()
    return email


def change_owner(account, membership_pk) -> str:
    from django.db import transaction

    from apps.accounts.models import Membership

    target = Membership.objects.filter(account=account, pk=membership_pk).select_related("user").first()
    if target is None:
        raise ConsoleError("That person isn't a member of this business.")
    with transaction.atomic():
        Membership.objects.filter(account=account, role=Membership.Role.OWNER).exclude(pk=target.pk) \
            .update(role=Membership.Role.ADMIN)
        target.role = Membership.Role.OWNER
        target.save(update_fields=["role"])
    return target.user.email or target.user.get_username()


def revoke_api_key(account, key_pk) -> str:
    from apps.email.models import EmailApiKey

    key = EmailApiKey.objects.filter(account=account, pk=key_pk, is_active=True).first()
    if key is None:
        raise ConsoleError("That key isn't active.")
    key.is_active = False
    key.save(update_fields=["is_active"])
    return f"…{key.last4}"


def cancel_job(account, job_pk) -> str:
    from apps.scheduler.api import SchedulingError
    from apps.scheduler.api import cancel_job as scheduler_cancel
    from apps.scheduler.models import ScheduledJob

    job = ScheduledJob.objects.filter(account=account, pk=job_pk).first()
    if job is None:
        raise ConsoleError("That job doesn't belong to this business.")
    try:
        scheduler_cancel(job)
    except SchedulingError as exc:
        raise ConsoleError(str(exc)) from exc
    return job.public_id

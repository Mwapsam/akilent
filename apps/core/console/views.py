"""Operator Console pages (/manage/). Every view is operator-only and every change is audited."""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import Account
from apps.billing.models import ManualPaymentRequest, PaymentMethod, Plan
from apps.core.audit import audit, label
from apps.core.console import attention, business, data
from apps.core.console.business import ConsoleError
from apps.core.forms import ConfigurationForm
from apps.core.models import AdminAction, Configurations, MailProviderSettings, SiteSettings
from apps.core.utils import admin_required
from apps.email.models import AuditLog, SendReputation
from apps.whatsapp.models import WebhookEventLog

User = get_user_model()
logger = logging.getLogger(__name__)

HEARTBEAT_LATE = timedelta(minutes=5)
QUIET_AFTER = timedelta(days=3)


def _late_queues(now):
    from apps.core.tasks import last_seen

    rows = []
    for queue in settings.WORKER_QUEUES:
        seen = last_seen(queue)
        rows.append({"name": queue, "seen": seen, "late": seen is None or now - seen > HEARTBEAT_LATE})
    return rows


# --- Home ---------------------------------------------------------------------------------------

@admin_required
def home(request):
    from apps.core import backups
    from apps.whatsapp import api as whatsapp_api

    now = timezone.now()
    rows = attention.businesses()
    queues = _late_queues(now)
    return render(request, "manage/home.html", {
        "total": len(rows),
        "needing": [r for r in rows if r["reasons"]][:15],
        "needing_count": sum(1 for r in rows if r["reasons"]),
        "pending_payments": ManualPaymentRequest.objects.filter(status=ManualPaymentRequest.PENDING).count(),
        "late_queues": [q for q in queues if q["late"]],
        "whatsapp": whatsapp_api.ops_status(),
        "backup": backups.status(),
        "recent_actions": [{"row": a, "label": label(a.action)}
                           for a in AdminAction.objects.select_related("actor", "account")[:8]],
    })


# --- Businesses ---------------------------------------------------------------------------------

@admin_required
def businesses(request):
    q = (request.GET.get("q") or "").strip()
    filter_key = request.GET.get("filter") or ""
    if filter_key not in attention.FILTERS:
        filter_key = ""
    return render(request, "manage/businesses.html", {
        "rows": attention.businesses(q=q, filter_key=filter_key),
        "q": q, "filter_key": filter_key, "filters": attention.FILTERS,
    })


@admin_required
def business_detail(request, pk, tab="overview"):
    account = get_object_or_404(Account, pk=pk)
    if tab not in business.TAB_DATA:
        tab = "overview"
    return render(request, "manage/business.html", {
        "business": account,
        "tab": tab,
        "tabs": business.TABS,
        "d": business.TAB_DATA[tab](account),
    })


def _business_redirect(account, tab):
    return redirect(reverse("core:business-tab", args=[account.pk, tab]))


# Each action: (tab to return to, handler(request, account) -> success message, audit action).
def _act_suspend(request, account):
    account.is_active = not account.is_active
    if account.is_active:
        account.scheduled_deletion_at = None
    account.save(update_fields=["is_active", "scheduled_deletion_at"])
    audit(request, "account.activate" if account.is_active else "account.suspend", account)
    return f"{account.company_name} is {'active again' if account.is_active else 'suspended'}."


def _act_subscription(request, account):
    result = business.set_subscription(account, request.POST.get("plan"), request.POST.get("status"))
    audit(request, "subscription.set", account, target=result)
    return f"Subscription set to {result}."


def _act_extend_trial(request, account):
    try:
        days = int(request.POST.get("days") or 7)
    except ValueError:
        raise ConsoleError("Enter a number of days.")
    result = business.extend_trial(account, days)
    audit(request, "subscription.extend_trial", account, target=f"+{days} days")
    return result + "."


def _act_module(request, account):
    module = request.POST.get("module", "")
    enabled = business.toggle_module(account, module)
    audit(request, "module.toggle", account, target=module, enabled=enabled)
    return f"{module.title()} is {'on' if enabled else 'off'}."


def _act_retry_registration(request, account):
    number = business.retry_registration(account, request.POST.get("number"))
    audit(request, "whatsapp.retry_registration", account, target=number)
    return f"{number} is registered."


def _act_sync_templates(request, account):
    result = business.sync_templates(account)
    audit(request, "whatsapp.sync_templates", account, synced=result.get("synced"))
    if result.get("errors"):
        raise ConsoleError("Couldn't reach WhatsApp for one or more numbers.")
    return f"Synced {result.get('synced', 0)} template(s)."


def _act_retry_send(request, account):
    business.retry_send(account, request.POST.get("message"))
    audit(request, "whatsapp.retry_send", account, target=request.POST.get("message"))
    return "Queued to send again."


def _act_resend_webhook(request, account):
    business.resend_webhook(account, request.POST.get("event"))
    audit(request, "whatsapp.resend_webhook", account, target=request.POST.get("event"))
    return "Webhook queued for processing."


def _act_reset_reputation(request, account):
    business.reset_reputation(account)
    audit(request, "email.reset_reputation", account)
    return "Email sending halt cleared."


def _act_reverify_domain(request, account):
    status = business.reverify_domain(account, request.POST.get("domain"))
    audit(request, "email.reverify_domain", account, target=request.POST.get("domain"))
    return f"Domain checked: {status}."


def _act_pause_workflow(request, account):
    name = business.pause_workflow(account, request.POST.get("workflow"))
    audit(request, "workflow.pause", account, target=name)
    return f"{name} is paused."


def _act_autopilot_off(request, account):
    business.ai_off(account, autopilot_only=True)
    audit(request, "ai.autopilot_off", account)
    return "AI autopilot is off: it will only suggest."


def _act_ai_off(request, account):
    business.ai_off(account, autopilot_only=False)
    audit(request, "ai.off", account)
    return "AI is off for this business."


def _act_resend_invitation(request, account):
    email = business.resend_invitation(request, account, request.POST.get("invitation"))
    audit(request, "invitation.resend", account, target=email)
    return f"Invitation sent again to {email}."


def _act_revoke_invitation(request, account):
    email = business.revoke_invitation(account, request.POST.get("invitation"))
    audit(request, "invitation.revoke", account, target=email)
    return f"Invitation to {email} revoked."


def _act_change_owner(request, account):
    who = business.change_owner(account, request.POST.get("membership"))
    audit(request, "team.change_owner", account, target=who)
    return f"{who} is now the owner."


def _act_revoke_key(request, account):
    key = business.revoke_api_key(account, request.POST.get("key"))
    audit(request, "api_key.revoke", account, target=key)
    return f"API key {key} revoked."


def _act_cancel_job(request, account):
    job = business.cancel_job(account, request.POST.get("job"))
    audit(request, "job.cancel", account, target=job)
    return f"Job {job} cancelled."


def _confirmed(request, account) -> None:
    if (request.POST.get("confirm") or "").strip() != account.slug:
        raise ConsoleError(f"Type the business's short name ({account.slug}) to confirm.")


def _act_delete_customer(request, account):
    _confirmed(request, account)
    who = (request.POST.get("who") or "").strip()
    result = data.delete_customer(account, who)
    if not result["contacts"] and not result["whatsapp"]:
        raise ConsoleError(f"No customer with {who!r} in this business.")
    audit(request, "data.delete_customer", account, target=who, **result)
    return f"Deleted {who}: {result['contacts']} contact record(s), {result['whatsapp']} WhatsApp record(s)."


def _act_close(request, account):
    _confirmed(request, account)
    data.close_account(account)
    audit(request, "account.close", account, deletes_on=account.scheduled_deletion_at.isoformat())
    return (f"{account.company_name} is closed and suspended. Everything will be deleted on "
            f"{timezone.localtime(account.scheduled_deletion_at):%d %b %Y} unless you reactivate it.")


ACTIONS = {
    "suspend": ("overview", _act_suspend),
    "subscription": ("billing", _act_subscription),
    "extend_trial": ("billing", _act_extend_trial),
    "module": ("billing", _act_module),
    "retry_registration": ("whatsapp", _act_retry_registration),
    "sync_templates": ("whatsapp", _act_sync_templates),
    "retry_send": ("whatsapp", _act_retry_send),
    "resend_webhook": ("whatsapp", _act_resend_webhook),
    "reset_reputation": ("email", _act_reset_reputation),
    "reverify_domain": ("email", _act_reverify_domain),
    "pause_workflow": ("automations", _act_pause_workflow),
    "autopilot_off": ("ai", _act_autopilot_off),
    "ai_off": ("ai", _act_ai_off),
    "resend_invitation": ("team", _act_resend_invitation),
    "revoke_invitation": ("team", _act_revoke_invitation),
    "change_owner": ("team", _act_change_owner),
    "revoke_key": ("api", _act_revoke_key),
    "cancel_job": ("api", _act_cancel_job),
    "delete_customer": ("data", _act_delete_customer),
    "close": ("data", _act_close),
}


@admin_required
@require_POST
def business_action(request, pk, action):
    account = get_object_or_404(Account, pk=pk)
    if action not in ACTIONS:
        messages.error(request, "Unknown action.")
        return _business_redirect(account, "overview")
    tab, handler = ACTIONS[action]
    try:
        messages.success(request, handler(request, account))
    except ConsoleError as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # show the operator what went wrong instead of a 500
        logger.exception("console action %s failed for account %s", action, account.pk)
        messages.error(request, f"That didn't work: {exc}")
    return _business_redirect(account, tab)


@admin_required
@require_POST
def business_export_contacts(request, pk):
    account = get_object_or_404(Account, pk=pk)
    audit(request, "data.export_contacts", account)
    response = HttpResponse(data.contacts_csv(account), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{account.slug}-contacts.csv"'
    return response


# --- View as (read-only support) ----------------------------------------------------------------

@admin_required
@require_POST
def view_as_start(request, pk):
    from apps.accounts.utils import start_view_as

    account = get_object_or_404(Account, pk=pk)
    start_view_as(request, account)
    audit(request, "view_as.start", account)
    return redirect("dashboard")


@admin_required
@require_POST
def view_as_stop(request):
    from apps.accounts.utils import stop_view_as

    account = stop_view_as(request)
    if account is not None:
        audit(request, "view_as.stop", account)
        return _business_redirect(account, "overview")
    return redirect("core:home")


# --- Old URLs -----------------------------------------------------------------------------------

@admin_required
def old_customers(request):
    return redirect("core:businesses")


@admin_required
def old_customer_detail(request, pk):
    return redirect("core:business", pk=pk)


@admin_required
def old_billing_requests(request):
    return redirect("core:payments")


# --- Payments and plans -------------------------------------------------------------------------

@admin_required
def payments(request):
    pending = (ManualPaymentRequest.objects.filter(status=ManualPaymentRequest.PENDING)
               .select_related("account", "plan").order_by("-created_at"))
    recent_resolved = (ManualPaymentRequest.objects.exclude(status=ManualPaymentRequest.PENDING)
                       .select_related("account", "plan", "reviewed_by").order_by("-reviewed_at")[:20])
    return render(request, "manage/billing_requests.html", {"pending": pending, "recent_resolved": recent_resolved})


@admin_required
def plans(request):
    return render(request, "manage/plans.html", {
        "plans": Plan.objects.order_by("price_monthly"),
        "payment_methods": PaymentMethod.objects.all(),
        "plan_service_type_choices": Plan.SERVICE_TYPE_CHOICES,
        "payments_enabled": SiteSettings.load().payments_enabled,
    })


# --- Platform -----------------------------------------------------------------------------------

@admin_required
def platform_health(request):
    return render(request, "manage/platform_health.html", {
        "reputation_issues": SendReputation.objects.exclude(state="ok").select_related("account").order_by("-state"),
        "audit_failures": AuditLog.objects.filter(success=False).select_related("account").order_by("-timestamp")[:50],
        "webhook_failures": WebhookEventLog.objects.filter(processed=False).order_by("-created_at")[:50],
    })


@admin_required
def pilot_command_center(request):
    """One page to check every morning during the pilot: is the platform healthy, and is each
    pilot business actually using Akilent? Read-only; every number comes from an app's api.py."""
    from apps.ai import api as ai_api
    from apps.automation import api as automation_api
    from apps.conversations import api as conversations_api
    from apps.core import backups
    from apps.whatsapp import api as whatsapp_api

    now = timezone.now()
    week = now - timedelta(days=7)
    rows = []
    for account, connected_at in whatsapp_api.connected_accounts():
        activity = conversations_api.activity(account, since=week)
        adoption = automation_api.adoption(account, since=connected_at)
        last_in = activity["last_customer_message_at"]
        rows.append({
            "account": account,
            "connected_at": connected_at,
            "first_value": adoption["first_run_at"] - connected_at if adoption["first_run_at"] else None,
            "automations_on": adoption["on"],
            "conversations_7d": activity["conversations"],
            "last_customer_message_at": last_in,
            "quiet": last_in is None or now - last_in > QUIET_AFTER,
            "ai": ai_api.usage_summary(account, since=week),
            "starting_point": conversations_api.starting_point(account, now),
        })

    return render(request, "manage/pilot.html", {
        "now": now,
        "queues": _late_queues(now),
        "whatsapp": whatsapp_api.ops_status(),
        "backup": backups.status(),
        "failed_runs": automation_api.recent_failed_runs(),
        "ai_errors_24h": ai_api.usage_summary(since=now - timedelta(hours=24))["errors"],
        "businesses": rows,
    })


@admin_required
def audit_log(request):
    rows = AdminAction.objects.select_related("actor", "account")
    action = request.GET.get("action") or ""
    if action:
        rows = rows.filter(action=action)
    return render(request, "manage/audit.html", {
        "rows": [{"row": r, "label": label(r.action)} for r in rows[:200]],
        "action": action,
        "actions": sorted({a for a in AdminAction.objects.values_list("action", flat=True).distinct()}),
        "label": label,
    })


# --- Settings -----------------------------------------------------------------------------------

@admin_required
def settings_page(request):
    site = SiteSettings.load()

    if request.method == "POST":
        site.app_name = (request.POST.get("app_name") or "Automator").strip() or "Automator"
        site.support_email = (request.POST.get("support_email") or "").strip()
        site.signups_enabled = "signups_enabled" in request.POST
        site.payments_enabled = "payments_enabled" in request.POST
        site.automation_events_enabled = "automation_events_enabled" in request.POST
        dp = request.POST.get("default_plan") or None
        site.default_plan = Plan.objects.filter(pk=dp).first() if dp else None
        try:
            site.default_trial_days = max(0, int(request.POST.get("default_trial_days") or 0))
        except ValueError:
            pass
        if request.FILES.get("logo"):
            site.logo = request.FILES["logo"]
        site.save()
        audit(request, "settings.site")
        messages.success(request, "Settings saved.")
        return redirect("core:settings")

    mail = MailProviderSettings.load()
    return render(request, "manage/settings.html", {
        "plans": Plan.objects.all().order_by("price_monthly"),
        "admins": User.objects.order_by("-is_superuser", "username"),
        "mail": mail,
        "percents": {  # stored as fractions, typed as percentages
            "bounce_warn": f"{mail.reputation_bounce_warn * 100:g}",
            "bounce_halt": f"{mail.reputation_bounce_halt * 100:g}",
            "complaint_halt": f"{mail.reputation_complaint_halt * 100:g}",
        },
        "whatsapp_env": settings.WHATSAPP_ENABLED,
    })


def _int(post, name, current, minimum=0):
    try:
        return max(minimum, int(post.get(name) or current))
    except ValueError:
        return current


def _rate(post, name, current):
    """A rate typed as a percentage ("5" = 5%), stored as a fraction (0.05)."""
    try:
        value = float(post.get(name))
    except (TypeError, ValueError):
        return current
    return min(max(value, 0.0), 100.0) / 100.0


@admin_required
@require_POST
def mail_settings_save(request):
    """Save mail provider settings (SES, SMTP, validation, consent, reputation thresholds)."""
    mail = MailProviderSettings.load()
    post = request.POST

    mail.infra_backend = post.get("infra_backend") or mail.infra_backend
    mail.send_backend = post.get("send_backend") or mail.send_backend
    mail.aws_region = (post.get("aws_region") or "").strip() or mail.aws_region
    mail.ses_configuration_set = (post.get("ses_configuration_set") or "").strip()
    mail.ses_sns_topic_arn = (post.get("ses_sns_topic_arn") or "").strip()
    mail.ses_send_rate_limit = _int(post, "ses_send_rate_limit", mail.ses_send_rate_limit, 1)
    mail.smtp_require_tls = "smtp_require_tls" in post
    mail.enable_recipient_validation = "enable_recipient_validation" in post
    mail.mx_validation_cache_ttl_seconds = _int(post, "mx_validation_cache_ttl_seconds", mail.mx_validation_cache_ttl_seconds)
    mail.soft_bounce_threshold = _int(post, "soft_bounce_threshold", mail.soft_bounce_threshold, 1)
    mail.require_explicit_consent = "require_explicit_consent" in post
    mail.reputation_bounce_warn = _rate(post, "reputation_bounce_warn", mail.reputation_bounce_warn)
    mail.reputation_bounce_halt = _rate(post, "reputation_bounce_halt", mail.reputation_bounce_halt)
    mail.reputation_complaint_halt = _rate(post, "reputation_complaint_halt", mail.reputation_complaint_halt)
    mail.reputation_min_volume = _int(post, "reputation_min_volume", mail.reputation_min_volume, 1)
    mail.reputation_window_hours = _int(post, "reputation_window_hours", mail.reputation_window_hours, 1)

    mail.save()
    audit(request, "settings.mail")
    messages.success(request, "Mail settings saved.")
    return redirect("core:settings")


@admin_required
@require_POST
def user_toggle_admin(request, pk):
    u = get_object_or_404(User, pk=pk)
    if u == request.user:
        messages.error(request, "You can't change your own operator access.")
        return redirect("core:settings")
    u.is_superuser = not u.is_superuser
    u.is_staff = u.is_superuser  # one admin flag: operators are superusers, and only they
    u.save(update_fields=["is_superuser", "is_staff"])
    audit(request, "operator.grant" if u.is_superuser else "operator.revoke", target=u.get_username())
    messages.success(
        request, f"{u.get_username()} is {'now an operator' if u.is_superuser else 'no longer an operator'}.")
    return redirect("core:settings")


# --- Configurations -----------------------------------------------------------------------------

@admin_required
def configurations_list(request):
    return render(request, "manage/configurations_list.html", {"configurations": Configurations.objects.all()})


@admin_required
def create_configuration(request):
    if request.method == "POST":
        form = ConfigurationForm(request.POST)
        if form.is_valid():
            config = form.save()
            audit(request, "configuration.create", target=config.name)
            messages.success(request, "Configuration created successfully.")
            return redirect("core:configurations-list")
    else:
        form = ConfigurationForm()
    return render(request, "manage/create_configuration.html", {"form": form})


@admin_required
def edit_configuration(request, pk):
    configuration = get_object_or_404(Configurations, pk=pk)
    if request.method == "POST":
        form = ConfigurationForm(request.POST, instance=configuration)
        if form.is_valid():
            form.save()
            audit(request, "configuration.edit", target=configuration.name)
            messages.success(request, "Configuration updated successfully.")
            return redirect("core:configurations-list")
        form.add_error_classes()
    else:
        form = ConfigurationForm(instance=configuration)
    return render(request, "manage/edit_configuration.html", {"form": form, "configuration": configuration})


@admin_required
def delete_configuration(request, pk):
    configuration = get_object_or_404(Configurations, pk=pk)
    if request.method == "POST":
        name = configuration.name
        configuration.delete()
        audit(request, "configuration.delete", target=name)
        messages.success(request, "Configuration deleted successfully.")
        return redirect("core:configurations-list")
    return render(request, "manage/delete_configuration.html", {"configuration": configuration})


@admin_required
def styleguide(request):
    """Living component gallery — the contract for every Akilent screen.

    Renders every design-system primitive so drift is visible and new screens have a reference.
    See docs/design/akilent-ui-spec.md §7. The chart samples include the two shapes that break naive
    scaling — a flat series and a single reading.
    """
    return render(request, "manage/styleguide.html", {
        "styleguide_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "styleguide_series": [
            ("Opens", [42, 58, 51, 74, 66, 31, 39]),
            ("Clicks", [8, 14, 11, 22, 19, 6, 9]),
        ],
        "styleguide_trend": [12, 18, 15, 24, 21, 30, 27, 34, 31, 42, 38, 47, 44, 52],
        "styleguide_flat": [4, 4, 4, 4, 4, 4],
        "styleguide_segments": [
            ("Succeeded", 842, "good"),
            ("Failed", 37, "critical"),
        ],
    })

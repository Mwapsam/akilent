"""Which businesses need an operator, and why, in plain words.

One list of reasons per business, used by the console home, the Businesses list and each
business page. Reads other apps' data inside functions (module boundary rule).
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

SETUP_GRACE = timedelta(days=3)
QUIET_AFTER = timedelta(days=3)
TRIAL_WARNING = timedelta(days=3)


def reasons(account, now=None) -> list[str]:
    from apps.accounts.models import Account
    from apps.billing.models import ManualPaymentRequest, Subscription
    from apps.email.models import SendReputation
    from apps.whatsapp.models import OutboundMessage
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

    now = now or timezone.now()
    out = []
    if not account.is_active:
        out.append("Suspended")

    try:
        sub = account.subscription  # one-to-one; missing raises
    except Subscription.DoesNotExist:
        sub = None
    if sub is None:
        out.append("No plan")
    elif sub.status in (Subscription.EXPIRED, Subscription.PAST_DUE, Subscription.INCOMPLETE):
        out.append(f"Subscription {sub.get_status_display().lower()}")
    elif sub.status == Subscription.TRIALING and sub.trial_ends_at and sub.trial_ends_at - now < TRIAL_WARNING:
        out.append(f"Trial ends {timezone.localtime(sub.trial_ends_at):%d %b}")

    if ManualPaymentRequest.objects.filter(account=account, status=ManualPaymentRequest.PENDING).exists():
        out.append("Payment waiting for approval")

    if account.onboarding_state != Account.Onboarding.COMPLETED and now - account.created_at > SETUP_GRACE:
        out.append("Setup not finished")

    numbers = list(WhatsAppBusinessNumber.objects.filter(account=account, is_active=True))
    if any(n.registration_status == WhatsAppBusinessNumber.RegistrationStatus.FAILED for n in numbers):
        out.append("WhatsApp registration failed")
    elif numbers and not any(n.is_ready for n in numbers):
        out.append("WhatsApp not ready")
    failed = OutboundMessage.objects.filter(
        account=account, status=OutboundMessage.Status.FAILED, updated_at__gte=now - timedelta(hours=24)).count()
    if failed:
        out.append(f"{failed} failed WhatsApp send{'s' if failed != 1 else ''} today")

    rep = SendReputation.objects.filter(account=account).first()
    if rep is not None and rep.state != "ok":
        out.append("Email sending halted" if rep.state == "halted" else "Email bounce warning")

    if numbers:
        from apps.conversations import api as conversations_api

        last_in = conversations_api.activity(account, since=now - QUIET_AFTER)["last_customer_message_at"]
        if last_in is None or now - last_in > QUIET_AFTER:
            out.append("Quiet: no customer message for 3+ days")
    return out


FILTERS = {
    "attention": "Needs attention",
    "trial": "On trial",
    "expired": "Expired or unpaid",
    "suspended": "Suspended",
    "whatsapp": "WhatsApp connected",
}


def businesses(*, q: str = "", filter_key: str = "") -> list[dict]:
    """Every business with its owner, plan and reasons, filtered and searched."""
    from django.db.models import Count, Q

    from apps.accounts.models import Account
    from apps.billing.models import Subscription

    rows = Account.objects.annotate(member_count=Count("memberships", distinct=True)).order_by("company_name")
    if q:
        rows = rows.filter(
            Q(company_name__icontains=q) | Q(slug__icontains=q)
            | Q(memberships__user__email__icontains=q)).distinct()
    if filter_key == "suspended":
        rows = rows.filter(is_active=False)
    elif filter_key == "trial":
        rows = rows.filter(subscription__status=Subscription.TRIALING)
    elif filter_key == "expired":
        rows = rows.filter(subscription__status__in=[
            Subscription.EXPIRED, Subscription.PAST_DUE, Subscription.INCOMPLETE, Subscription.CANCELLED])
    elif filter_key == "whatsapp":
        rows = rows.filter(whatsapp_numbers__is_active=True).distinct()

    now = timezone.now()
    out = []
    for account in rows.select_related("subscription__plan"):
        why = reasons(account, now)
        if filter_key == "attention" and not why:
            continue
        try:
            sub = account.subscription
        except Subscription.DoesNotExist:
            sub = None
        out.append({"account": account, "owner": account.owner, "members": account.member_count,
                    "subscription": sub, "reasons": why})
    return out

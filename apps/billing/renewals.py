"""The renewal engine: Akilent, not the payment provider, decides when a period ends and what
happens next. Runs hourly (tasks.renew_due_subscriptions).

For each paid subscription whose period has ended:

1. Cancelled at period end: it becomes CANCELLED.
2. Otherwise a renewal payment is created (idempotent per period). Credit is used first; if it
   covers the period, the subscription is renewed with no charge.
3. Otherwise the active provider charges the saved payment method, if it can and one exists for
   that provider. A method saved with a different (previous) provider isn't used: that is how a
   provider switch migrates customers at their next renewal.
4. Otherwise (or if the charge fails) the subscription goes PAST_DUE and the owner is emailed a
   link to pay with the active provider. Features are kept for BILLING_GRACE_DAYS, then it
   EXPIRES.

Subscriptions still renewed by a provider-side subscription from before this engine (legacy
stripe_subscription_id / fw_subscription_id) are skipped until migrate_legacy_billing moves them.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import payments
from .models import BillingPayment, Subscription
from .providers import ProviderError, registry

logger = logging.getLogger(__name__)

UPCOMING_NOTICE = timedelta(days=3)
# Days after a renewal went unpaid on which a reminder is sent.
REMINDER_DAYS = (3, 6)
FREE = "free"


def grace_period() -> timedelta:
    return timedelta(days=getattr(settings, "BILLING_GRACE_DAYS", 7))


def _engine_managed():
    return Subscription.objects.filter(
        Q(stripe_subscription_id__isnull=True) | Q(stripe_subscription_id=""),
        Q(fw_subscription_id__isnull=True) | Q(fw_subscription_id=""),
    )


def run(now=None) -> dict:
    now = now or timezone.now()
    counts = {"renewed": 0, "charged": 0, "past_due": 0, "cancelled": 0, "expired": 0}
    due = _engine_managed().filter(
        status__in=[Subscription.ACTIVE, Subscription.PAST_DUE],
        current_period_end__lte=now,
    )
    for pk in list(due.values_list("pk", flat=True)):
        try:
            outcome = renew(pk, now=now)
        except Exception:
            logger.exception("renewals: subscription %s failed", pk)
            continue
        if outcome in counts:
            counts[outcome] += 1
    send_due_notices(now)
    return counts


def renew(subscription_id: int, *, now=None) -> str:
    """Move one due subscription on. Returns what happened (a key of run()'s counts, or "")."""
    now = now or timezone.now()
    with transaction.atomic():
        sub = (
            Subscription.objects.select_for_update()
            .select_related("plan", "account")
            .get(pk=subscription_id)
        )
        if (
            sub.status not in (Subscription.ACTIVE, Subscription.PAST_DUE)
            or not sub.current_period_end
            or sub.current_period_end > now
        ):
            return ""
        if sub.cancel_at_period_end:
            sub.status = Subscription.CANCELLED
            sub.cancelled_at = now
            sub.save(update_fields=["status", "cancelled_at", "updated_at"])
            _notify(sub, "cancelled")
            return "cancelled"
        if sub.status == Subscription.PAST_DUE and sub.past_due_since:
            if now - sub.past_due_since >= grace_period():
                sub.status = Subscription.EXPIRED
                sub.save(update_fields=["status", "updated_at"])
                _notify(sub, "expired")
                return "expired"
        if sub.plan.price_monthly <= 0:
            from .services import activate_subscription

            activate_subscription(
                sub.account, sub.plan, FREE, billing_period=sub.billing_period
            )
            return "renewed"

    provider = registry.active()
    payment = payments.create_payment(
        sub.account,
        sub.plan,
        sub.billing_period,
        provider=provider.code if provider else "",
        purpose=BillingPayment.RENEWAL,
        reference=f"renew_{sub.pk}_{int(sub.current_period_end.timestamp())}",
    )
    if payment.status == BillingPayment.SUCCEEDED:
        if payment.provider == payments.CREDIT:
            _notify(sub, "renewed", payment)
        return "renewed"
    if payment.status != BillingPayment.PENDING:
        return ""  # this period's attempt already happened; waiting on the customer or grace
    # Exactly one run gets to charge this payment; an overlapping run stops here.
    if not payments.claim_for_charge(payment):
        return ""

    method = (
        payments.default_method(sub.account, provider.code)
        if provider and provider.off_session_charge
        else None
    )
    if method is None or provider is None:
        payments.fail(payment, "No saved payment method for this provider")
        return "past_due"
    try:
        result = provider.charge_saved_method(method, payment)
    except ProviderError as exc:
        logger.warning(
            "renewals: %s charge failed for sub=%s: %s", provider.code, sub.pk, exc
        )
        payments.fail(payment, str(exc))
        return "past_due"
    payment = payments.record_result(provider.code, result) or payment
    if payment.status == BillingPayment.SUCCEEDED:
        _notify(sub, "renewed", payment)
        return "charged"
    # Still PENDING: the provider is deciding and its webhook will follow. If it never does,
    # payments.abandon_stale treats the charge as unpaid after a day.
    return "" if payment.status == BillingPayment.PENDING else "past_due"


def send_due_notices(now=None) -> None:
    """Heads-up a few days before a paid renewal, and reminders while a renewal is unpaid."""
    now = now or timezone.now()
    upcoming = _engine_managed().filter(
        status=Subscription.ACTIVE,
        cancel_at_period_end=False,
        plan__price_monthly__gt=0,
        current_period_end__gt=now,
        current_period_end__lte=now + UPCOMING_NOTICE,
    )
    for sub in upcoming.select_related("plan", "account"):
        if sub.renewal_notice_for == sub.current_period_end:
            continue
        sub.renewal_notice_for = sub.current_period_end
        sub.save(update_fields=["renewal_notice_for", "updated_at"])
        _notify(sub, "upcoming")

    for sub in Subscription.objects.filter(
        status=Subscription.PAST_DUE, past_due_since__isnull=False
    ).select_related("plan", "account"):
        days = (now - sub.past_due_since).days
        sent = sub.dunning_notices_sent
        if sent < len(REMINDER_DAYS) and days >= REMINDER_DAYS[sent]:
            sub.dunning_notices_sent = sent + 1
            sub.save(update_fields=["dunning_notices_sent", "updated_at"])
            _notify(sub, "reminder")


def _notify(sub, kind, payment=None) -> None:
    from .tasks import send_billing_notice

    payment_id = payment.pk if payment else None
    transaction.on_commit(
        lambda: send_billing_notice.delay(sub.account_id, kind, payment_id)
    )

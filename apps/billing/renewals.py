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

A subscription renewed by a provider-run recurring subscription (``recurring_id``, a PayPal
subscription) is charged by the provider on its own schedule; each payment it reports renews
the period. Here it only waits: a renewal that hasn't arrived RECURRING_GRACE after the period
ended counts as unpaid (past due, then grace, then expiry, which stops it at the provider).

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

from . import payments, recurring
from .models import BillingPayment, Subscription
from .providers import ProviderError, registry

logger = logging.getLogger(__name__)

UPCOMING_NOTICE = timedelta(days=3)
# How long after the period ends a provider-run renewal may still arrive before it counts as
# unpaid (the provider charges at its own time of day, and two years can be a day longer).
RECURRING_GRACE = timedelta(days=2)
# Days after a renewal went unpaid on which a reminder is sent.
REMINDER_DAYS = (3, 6)
# Days after a paid signup that wasn't paid for on which a reminder to finish paying is sent;
# signups older than FINISH_PAYMENT_WINDOW are left alone (never a surprise email months on).
FINISH_PAYMENT_DAYS = (1, 3)
FINISH_PAYMENT_WINDOW = timedelta(days=7)
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
            recurring.retire(sub)
            sub.status = Subscription.CANCELLED
            sub.cancelled_at = now
            sub.save()
            _notify(sub, "cancelled")
            return "cancelled"
        if sub.status == Subscription.PAST_DUE and sub.past_due_since:
            if now - sub.past_due_since >= grace_period():
                recurring.retire(sub)
                sub.status = Subscription.EXPIRED
                sub.save()
                _notify(sub, "expired")
                return "expired"
        if sub.recurring_id:
            return _await_provider(sub, now)
        if sub.plan.price_monthly <= 0:
            from .services import activate_subscription

            activate_subscription(
                sub.account,
                sub.plan,
                FREE,
                billing_period=sub.billing_period,
                anchor=sub.current_period_end,
            )
            return "renewed"

    provider = registry.active()
    payment = payments.create_payment(
        sub.account,
        sub.plan,
        sub.billing_period,
        provider=provider.code if provider else "",
        purpose=BillingPayment.RENEWAL,
        reference=payments.renewal_reference(sub),
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
        # Nothing was tried: the customer renews by pay link. Their email says the renewal
        # is due, not that a payment failed.
        payments.fail(payment, payments.NOT_CHARGED, notice="due")
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


def _await_provider(sub, now) -> str:
    """The provider charges this one itself; a renewal it hasn't started by RECURRING_GRACE
    after the period ended counts as unpaid."""
    if (
        sub.status == Subscription.PAST_DUE
        or now < sub.current_period_end + RECURRING_GRACE
    ):
        return ""
    if payments.has_unconfirmed_charge(sub.account):
        return (
            ""  # the provider took a payment that is still settling (e.g. under review)
        )
    payments.mark_past_due(sub.account)
    return "past_due"


def renews_automatically(sub) -> bool:
    """Will something take ``sub``'s next renewal without the customer: a provider-run
    recurring subscription, or the active provider charging a saved method? Otherwise the
    customer pays by link."""
    if sub is None:
        return False
    if sub.recurring_id:
        return True
    provider = registry.active()
    return bool(
        provider is not None
        and provider.off_session_charge
        and payments.default_method(sub.account, provider.code)
    )


def send_due_notices(now=None) -> None:
    """Heads-up a few days before a paid renewal or a trial's end, and reminders while a
    renewal is unpaid."""
    now = now or timezone.now()
    ending_trials = Subscription.objects.filter(
        status=Subscription.TRIALING,
        trial_ends_at__gt=now,
        trial_ends_at__lte=now + UPCOMING_NOTICE,
    )
    for sub in ending_trials.select_related("plan", "account"):
        if sub.renewal_notice_for == sub.trial_ends_at:
            continue
        sub.renewal_notice_for = sub.trial_ends_at
        sub.save(update_fields=["renewal_notice_for", "updated_at"])
        _notify(sub, "trial_ending")

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
            _notify(sub, _reminder_kind(sub))

    send_finish_payment_reminders(now)


def send_finish_payment_reminders(now=None) -> int:
    """A business that signed up for a paid plan and didn't pay (left at the provider's page,
    or closed the tab) is reminded on FINISH_PAYMENT_DAYS after signup, with a link back to
    its payment page. Not while a payment may already be on its way (in flight at the
    provider, or a bank transfer waiting for review): paying again could pay twice. Not when
    there is no way to pay right now. Returns how many were sent."""
    from apps.core.models import SiteSettings

    from .models import ManualPaymentRequest

    now = now or timezone.now()
    if not SiteSettings.load().payments_enabled or (
        registry.active() is None and not registry.manual_enabled()
    ):
        return 0  # a reminder with nothing to pay with would be a dead end
    sent_count = 0
    for sub in Subscription.objects.filter(
        status=Subscription.INCOMPLETE,
        created_at__gt=now - FINISH_PAYMENT_WINDOW,
        dunning_notices_sent__lt=len(FINISH_PAYMENT_DAYS),
        plan__is_active=True,
        plan__price_monthly__gt=0,
        account__is_active=True,
    ).select_related("plan", "account"):
        sent = sub.dunning_notices_sent
        if now - sub.created_at < timedelta(days=FINISH_PAYMENT_DAYS[sent]):
            continue
        if payments.has_unconfirmed_charge(sub.account) or (
            ManualPaymentRequest.objects.filter(
                account_id=sub.account_id, status=ManualPaymentRequest.PENDING
            ).exists()
        ):
            continue
        # Only the run that moves the count on sends, so overlapping runs send once.
        moved = Subscription.objects.filter(
            pk=sub.pk,
            status=Subscription.INCOMPLETE,
            dunning_notices_sent=sent,
        ).update(dunning_notices_sent=sent + 1, updated_at=now)
        if moved:
            _notify(sub, "finish_payment")
            sent_count += 1
    return sent_count


def _reminder_kind(sub) -> str:
    """The reminder to send: ``due_reminder`` when this period's renewal was never charged
    (a pay-link customer), else ``reminder`` (a payment was tried and didn't go through)."""
    if sub.recurring_id or not sub.current_period_end:
        return "reminder"
    payment = BillingPayment.objects.filter(
        reference=payments.renewal_reference(sub)
    ).first()
    if payment is not None and payment.failure_reason == payments.NOT_CHARGED:
        return "due_reminder"
    return "reminder"


def _notify(sub, kind, payment=None) -> None:
    from .tasks import send_billing_notice

    payment_id = payment.pk if payment else None
    transaction.on_commit(
        lambda: send_billing_notice.delay(sub.account_id, kind, payment_id)
    )

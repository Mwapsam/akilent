"""Is billing keeping its dates? A daily check (tasks.billing_health_check) and
``manage.py billing_check`` list anything that should have moved on and hasn't, in plain words
for an operator.

Every date in billing is acted on by a scheduled job (renewals and trials hourly, PayPal sync
daily). If Celery beat stops, nothing fails loudly: renewals simply don't happen. This is the
alarm for that, and for payments stuck between states.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# How late a date may be before it counts as missed: the jobs run hourly, so anything later
# than this means a job isn't running (or keeps failing for that subscription).
LATE = timedelta(hours=3)
# A recurring subscription still waiting to be stopped at its provider after this long.
STOP_LATE = timedelta(days=1)
# How far back a paid payment is checked against the plan it should have switched on.
PAID_LOOKBACK = timedelta(days=2)


def problems(now=None) -> list[str]:
    from . import payments, renewals
    from .models import BillingPayment, Subscription

    now = now or timezone.now()
    found: list[str] = []
    engine = renewals._engine_managed()

    def who(sub) -> str:
        return f"{sub.account} (account {sub.account_id})"

    # Paid periods whose end passed and that the renewal engine never picked up.
    for sub in engine.filter(
        status=Subscription.ACTIVE,
        recurring_id="",
        current_period_end__lt=now - LATE,
    ).select_related("account"):
        payment = BillingPayment.objects.filter(
            reference=payments.renewal_reference(sub)
        ).first()
        if payment is not None and payment.status == BillingPayment.PENDING:
            continue  # charged and waiting for the provider; abandon_stale watches it
        found.append(
            f"Renewal missed: {who(sub)} period ended "
            f"{timezone.localtime(sub.current_period_end):%d %b %Y %H:%M} and is still active."
        )
    for sub in (
        engine.filter(
            status=Subscription.ACTIVE,
            current_period_end__lt=now - renewals.RECURRING_GRACE - LATE,
        )
        .exclude(recurring_id="")
        .select_related("account")
    ):
        if payments.has_unconfirmed_charge(sub.account):
            continue
        found.append(
            f"Automatic renewal not received: {who(sub)} (provider subscription "
            f"{sub.recurring_id}) period ended "
            f"{timezone.localtime(sub.current_period_end):%d %b %Y} with no payment."
        )

    for sub in Subscription.objects.filter(
        status=Subscription.TRIALING, trial_ends_at__lt=now - LATE
    ).select_related("account"):
        found.append(
            f"Trial not ended: {who(sub)} trial ended "
            f"{timezone.localtime(sub.trial_ends_at):%d %b %Y %H:%M}."
        )

    grace = renewals.grace_period()
    for sub in Subscription.objects.filter(status=Subscription.PAST_DUE).select_related(
        "account"
    ):
        if sub.past_due_since is None:
            found.append(f"Past due with no start date (never expires): {who(sub)}.")
        elif sub.past_due_since + grace < now - LATE:
            found.append(
                f"Past grace and not expired: {who(sub)} unpaid since "
                f"{timezone.localtime(sub.past_due_since):%d %b %Y}."
            )

    for sub in (
        Subscription.objects.exclude(recurring_stop_id="")
        .filter(updated_at__lt=now - STOP_LATE)
        .select_related("account")
    ):
        found.append(
            f"Automatic payments not stopped at the provider: {who(sub)} "
            f"({sub.recurring_stop_id}). The customer may still be charged."
        )

    for payment in BillingPayment.objects.filter(
        resolution=BillingPayment.UNRESOLVED
    ).select_related("account"):
        found.append(
            f"Payment outcome unknown: {payment.reference} ({payment.provider}, "
            f"{payment.amount_due} {payment.currency}) for {payment.account}."
        )

    for payment in BillingPayment.objects.filter(
        status=BillingPayment.SUCCEEDED,
        paid_at__gte=now - PAID_LOOKBACK,
        amount_due__gt=0,
    ).select_related("account"):
        sub = Subscription.objects.filter(account_id=payment.account_id).first()
        if sub is None or sub.status not in (
            Subscription.ACTIVE,
            Subscription.PAST_DUE,
            Subscription.CANCELLED,  # cancelled since paying: the operator's call
        ):
            found.append(
                f"Paid but plan not on: {payment.reference} ({payment.amount_due} "
                f"{payment.currency}) for {payment.account}, plan is "
                f"{sub.get_status_display() if sub else 'missing'}."
            )
    return found


def alert(found: list[str]) -> None:
    """Email superusers and post to Slack. Either alone is enough, so one failing never
    stops the other."""
    from django.contrib.auth.models import User

    from apps.email.services.send import send_system_email

    from .slack import post_message

    subject = f"Billing check: {len(found)} problem{'s' if len(found) != 1 else ''}"
    body = "\n".join(f"- {line}" for line in found)
    for email in (
        User.objects.filter(is_superuser=True, is_active=True)
        .exclude(email="")
        .values_list("email", flat=True)
    ):
        try:
            send_system_email(email, subject, text_body=body, sender_label="Billing")
        except Exception:
            logger.exception("billing health: email to %s failed", email)
    try:
        post_message(f":rotating_light: *{subject}*\n{body}")
    except Exception:
        logger.exception("billing health: slack post failed")
    logger.error("%s\n%s", subject, body)

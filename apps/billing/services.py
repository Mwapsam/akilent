from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import Subscription
from .money import quantize
from .pricing import period_days, price_for

ZERO = Decimal("0.00")


@transaction.atomic
def activate_subscription(
    account,
    plan,
    provider: str,
    *,
    billing_period: str = Subscription.MONTHLY,
) -> Subscription:
    """Start (or renew) a paid period of ``plan`` for ``account``.

    The one place a period is granted, whichever provider took the money (or credit covered it).
    Paying again for the same plan and period while still in a paid period (a renewal, or paying
    early) extends from the current period end, so no paid day is lost. Anything else starts a
    fresh period now; the unused part of the old one was already taken off the price of this
    payment (``unused_value``, applied in payments.create_payment).
    """
    now = timezone.now()
    sub = Subscription.objects.select_for_update().filter(account=account).first()
    start = period_start = now
    if (
        sub is not None
        and sub.current_period_end is not None
        and _in_paid_period(sub, now)
        and sub.plan_id == plan.pk
        and sub.billing_period == billing_period
    ):
        start, period_start = sub.current_period_end, sub.current_period_start

    fields = {
        "plan": plan,
        "status": Subscription.ACTIVE,
        "billing_period": billing_period,
        "current_period_start": period_start,
        "current_period_end": start + timedelta(days=period_days(billing_period)),
        "provider": provider,
        "trial_ends_at": None,
        "cancelled_at": None,
        "cancel_at_period_end": False,
        "past_due_since": None,
        "dunning_notices_sent": 0,
    }
    if sub is None:
        return Subscription.objects.create(account=account, **fields)
    for key, value in fields.items():
        setattr(sub, key, value)
    sub.save()
    return sub


def _in_paid_period(sub, now) -> bool:
    return bool(
        sub.status in (Subscription.ACTIVE, Subscription.PAST_DUE)
        and sub.current_period_end
        and sub.current_period_end > now
    )


def _term_price_paid(sub) -> Decimal:
    """What one term of ``sub``'s plan and period cost the customer: the gross amount of the
    latest paid payment for that plan and period less anything refunded from it, else
    today's price_for (an operator-set plan, or a term paid before payments were recorded).
    Gross, before credit and unused time, because those were the customer's own money too."""
    from apps.billing.models import BillingPayment

    paid = (
        BillingPayment.objects.filter(
            account_id=sub.account_id,
            plan_id=sub.plan_id,
            billing_period=sub.billing_period,
            status=BillingPayment.SUCCEEDED,
            gross_amount__gt=0,
        )
        .order_by("-paid_at", "-pk")
        .values_list("gross_amount", "refunded_amount")
        .first()
    )
    if paid is None:
        return price_for(sub.plan, sub.billing_period)
    gross, refunded = paid
    return max(ZERO, gross - (refunded or ZERO))


def unused_value(sub, plan, billing_period, now=None) -> Decimal:
    """What's left of the paid time on ``sub`` when it moves to a different ``plan`` or
    ``billing_period`` (zero when it's the same plan and period, which extends instead).

    Valued per day at what the customer was charged for their current plan and term (the
    latest paid payment for it), so nobody is credited less than they paid when prices
    change; today's price only when no payment is on record. A period already extended by
    an early renewal is valued in full. Only time somebody paid for counts: a plan an
    operator set by hand records no provider.
    """
    now = now or timezone.now()
    if (
        sub is None
        or not _in_paid_period(sub, now)
        or sub.status != Subscription.ACTIVE
    ):
        return ZERO
    if sub.plan_id == plan.pk and sub.billing_period == billing_period:
        return ZERO
    if not (sub.provider or sub.payment_method) or sub.plan.price_monthly <= 0:
        return ZERO
    per_day = _term_price_paid(sub) / Decimal(period_days(sub.billing_period))
    days_left = Decimal((sub.current_period_end - now).total_seconds()) / Decimal(86400)
    return max(ZERO, quantize(per_day * days_left))

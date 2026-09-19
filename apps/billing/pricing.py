from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from .models import Subscription

# Multiplier applied to Plan.price_monthly x months-in-period for each billing
# period. 1.00 = no discount. Overridable via settings.BILLING_PERIOD_MULTIPLIERS
# for environment-specific promos without a code change.
DEFAULT_PERIOD_MULTIPLIERS = {
    Subscription.MONTHLY: Decimal("1.00"),
    Subscription.QUARTERLY: Decimal("0.95"),
    Subscription.ANNUALLY: Decimal("0.85"),
    Subscription.BIENNIAL: Decimal("0.75"),
}

PERIOD_DAYS = {
    Subscription.MONTHLY: 30,
    Subscription.QUARTERLY: 91,
    Subscription.ANNUALLY: 365,
    Subscription.BIENNIAL: 730,
}

# How many "months" of price_monthly the period represents, before discount.
PERIOD_MONTH_MULTIPLES = {
    Subscription.MONTHLY: Decimal("1"),
    Subscription.QUARTERLY: Decimal("3"),
    Subscription.ANNUALLY: Decimal("12"),
    Subscription.BIENNIAL: Decimal("24"),
}

# (interval, interval_count) per Stripe's price.recurring API.
STRIPE_INTERVAL = {
    Subscription.MONTHLY: ("month", 1),
    Subscription.QUARTERLY: ("month", 3),
    Subscription.ANNUALLY: ("year", 1),
    Subscription.BIENNIAL: ("year", 2),
}


def _multipliers() -> dict:
    return getattr(settings, "BILLING_PERIOD_MULTIPLIERS", DEFAULT_PERIOD_MULTIPLIERS)


def price_for(plan, period: str) -> Decimal:
    """Total amount to charge for one full billing_period of `plan`."""
    months = PERIOD_MONTH_MULTIPLES[period]
    multiplier = _multipliers()[period]
    return (plan.price_monthly * months * multiplier).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def period_days(period: str) -> int:
    return PERIOD_DAYS[period]


def stripe_interval(period: str):
    return STRIPE_INTERVAL[period]

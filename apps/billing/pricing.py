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

# How a price for one period is written after the amount: "$99 / month", "$1,782 / 2 years".
PERIOD_PER = {
    Subscription.MONTHLY: "month",
    Subscription.QUARTERLY: "quarter",
    Subscription.ANNUALLY: "year",
    Subscription.BIENNIAL: "2 years",
}

# (interval, interval_count) per Stripe's price.recurring API.
STRIPE_INTERVAL = {
    Subscription.MONTHLY: ("month", 1),
    Subscription.QUARTERLY: ("month", 3),
    Subscription.ANNUALLY: ("year", 1),
    Subscription.BIENNIAL: ("year", 2),
}


def multiplier_for(period: str) -> Decimal:
    """The price multiplier for ``period``. ``settings.BILLING_PERIOD_MULTIPLIERS`` may
    override any of them (a promo); a period it leaves out keeps its default."""
    override = getattr(settings, "BILLING_PERIOD_MULTIPLIERS", None) or {}
    return Decimal(str(override.get(period, DEFAULT_PERIOD_MULTIPLIERS[period])))


def price_for(plan, period: str) -> Decimal:
    """Total amount to charge for one full billing_period of `plan`."""
    months = PERIOD_MONTH_MULTIPLES[period]
    return (plan.price_monthly * months * multiplier_for(period)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def period_options() -> list[dict]:
    """Every billing period, described once for every page that offers a choice of term
    (signup, Billing -> Plans): its label, how a price for it is written, how many months
    it covers, its multiplier and the saving that gives. Adding a period means adding it
    to Subscription.BILLING_PERIOD_CHOICES and to the tables above; nothing else."""
    options = []
    for value, label in Subscription.BILLING_PERIOD_CHOICES:
        multiplier = multiplier_for(value)
        options.append(
            {
                "value": value,
                "label": label,
                "per": PERIOD_PER[value],
                "months": int(PERIOD_MONTH_MULTIPLES[value]),
                "multiplier": float(multiplier),
                "savePct": round((1 - float(multiplier)) * 100),
            }
        )
    return options


def period_days(period: str) -> int:
    return PERIOD_DAYS[period]


def stripe_interval(period: str):
    return STRIPE_INTERVAL[period]

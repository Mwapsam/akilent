from decimal import Decimal

from apps.billing.pricing import period_days, price_for, stripe_interval


def test_price_for_monthly_is_unchanged(plan):
    assert price_for(plan, "monthly") == plan.price_monthly


def test_price_for_applies_period_discounts(plan):
    # plan.price_monthly == 19.00
    assert price_for(plan, "quarterly") == Decimal("54.15")  # 19 * 3 * 0.95
    assert price_for(plan, "annually") == Decimal("193.80")  # 19 * 12 * 0.85
    assert price_for(plan, "biennial") == Decimal("342.00")  # 19 * 24 * 0.75


def test_period_days_covers_all_periods():
    assert period_days("monthly") == 30
    assert period_days("quarterly") == 91
    assert period_days("annually") == 365
    assert period_days("biennial") == 730


def test_stripe_interval_mapping():
    assert stripe_interval("monthly") == ("month", 1)
    assert stripe_interval("quarterly") == ("month", 3)
    assert stripe_interval("annually") == ("year", 1)
    assert stripe_interval("biennial") == ("year", 2)

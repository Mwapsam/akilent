from decimal import Decimal

from apps.billing.money import format_price
from apps.billing.pricing import (
    monthly_equivalent,
    period_days,
    price_for,
    stripe_interval,
)


def test_price_for_monthly_is_unchanged(plan):
    assert price_for(plan, "monthly") == plan.price_monthly


def test_price_for_applies_period_discounts_in_whole_units(plan):
    # plan.price_monthly == 19.00. A longer term is charged in whole units, so "billed
    # $194 a year" is exactly what checkout and renewals take.
    assert price_for(plan, "quarterly") == Decimal("54")  # 19 * 3 * 0.95 = 54.15
    assert price_for(plan, "annually") == Decimal("194")  # 19 * 12 * 0.85 = 193.80
    assert price_for(plan, "biennial") == Decimal("342")  # 19 * 24 * 0.75


def test_monthly_keeps_its_cents(plan):
    plan.price_monthly = Decimal("11.90")
    assert price_for(plan, "monthly") == Decimal("11.90")
    assert monthly_equivalent(plan, "monthly") == Decimal("11.90")
    # 11.90 * 12 * 0.85 = 121.38 -> 121; per month 10.08 -> 10.
    assert price_for(plan, "annually") == Decimal("121")
    assert monthly_equivalent(plan, "annually") == Decimal("10")


def test_monthly_equivalent_is_the_whole_term_spread_and_rounded(plan):
    assert monthly_equivalent(plan, "annually") == Decimal("16")  # 194 / 12 = 16.17
    assert monthly_equivalent(plan, "quarterly") == Decimal("18")  # 54 / 3
    assert monthly_equivalent(plan, "biennial") == Decimal("14")  # 342 / 24 = 14.25


def test_format_price_drops_only_a_whole_amounts_cents(settings):
    settings.BILLING_CURRENCY = "USD"
    assert format_price(Decimal("194")) == "$194"
    assert format_price(Decimal("1009")) == "$1,009"
    assert format_price(Decimal("11.90")) == "$11.90"
    settings.BILLING_CURRENCY = "ZMW"
    assert format_price(Decimal("49")) == "K49"


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

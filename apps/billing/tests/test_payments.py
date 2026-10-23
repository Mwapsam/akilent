"""The provider-neutral payment core: idempotency, amount/currency checks, plan changes."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing import credit, payments
from apps.billing.models import BillingPayment, CreditGrant, Plan, Subscription
from apps.billing.providers import ProviderResult

D = Decimal


@pytest.mark.django_db
def test_record_result_is_idempotent(account, plan, subscription, fake_provider):
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.record_result("fake", fake_provider.paid(payment))
    subscription.refresh_from_db()
    end = subscription.current_period_end
    payments.record_result(
        "fake", fake_provider.paid(payment)
    )  # webhook after the return
    subscription.refresh_from_db()
    assert subscription.current_period_end == end


@pytest.mark.django_db
def test_result_from_another_provider_is_ignored(
    account, plan, subscription, fake_provider
):
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    assert payments.record_result("stripe", fake_provider.paid(payment)) is None
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING


@pytest.mark.django_db
def test_currency_mismatch_fails(account, plan, subscription, fake_provider):
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.record_result(
        "fake",
        ProviderResult(
            reference=payment.reference,
            status="succeeded",
            amount=D("19"),
            currency="NGN",
        ),
    )
    payment.refresh_from_db()
    assert payment.status == BillingPayment.FAILED


@pytest.mark.django_db
def test_renewing_early_extends_from_period_end(
    account, plan, subscription, fake_provider
):
    end = timezone.now() + timedelta(days=10)
    subscription.status = Subscription.ACTIVE
    subscription.provider = "fake"
    subscription.current_period_end = end
    subscription.save()
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.record_result("fake", fake_provider.paid(payment))
    subscription.refresh_from_db()
    assert subscription.current_period_end == end + timedelta(days=30)


@pytest.mark.django_db
def test_upgrade_takes_unused_time_off_the_price(
    account, plan, subscription, fake_provider
):
    """Half a $19 month left: moving to a $40 plan costs $40 - $9.50."""
    now = timezone.now()
    subscription.status = Subscription.ACTIVE
    subscription.provider = "fake"
    subscription.current_period_start = now - timedelta(days=15)
    subscription.current_period_end = now + timedelta(days=15)
    subscription.save()
    pro = Plan.objects.create(slug="pro", name="Pro", price_monthly=D("40"))

    payment = payments.create_payment(account, pro, "monthly", provider="fake")
    assert abs(payment.amount_due - D("30.50")) <= D("0.02")
    assert not CreditGrant.objects.exists()  # nothing is granted until it's paid


@pytest.mark.django_db
def test_unused_time_after_an_early_renewal_is_valued_in_full(
    account, plan, subscription, fake_provider
):
    """Two paid months (an early renewal) then a downgrade: both months count."""
    now = timezone.now()
    subscription.status = Subscription.ACTIVE
    subscription.provider = "fake"
    subscription.current_period_start = now - timedelta(days=1)
    subscription.current_period_end = now + timedelta(days=59)
    subscription.save()
    cheap = Plan.objects.create(slug="mini", name="Mini", price_monthly=D("5"))

    payment = payments.create_payment(account, cheap, "monthly", provider="fake")
    assert payment.amount_due == 0  # covered by unused time
    excess = credit.balance(account)
    assert abs(excess - (D("19") / 30 * 59 - D("5"))) <= D("0.05")


@pytest.mark.django_db
def test_operator_set_plan_has_no_unused_value(
    account, plan, subscription, fake_provider
):
    subscription.status = Subscription.ACTIVE
    subscription.current_period_end = timezone.now() + timedelta(days=20)
    subscription.save()  # no provider recorded: nobody paid
    pro = Plan.objects.create(slug="pro", name="Pro", price_monthly=D("40"))
    payment = payments.create_payment(account, pro, "monthly", provider="fake")
    assert payment.amount_due == D("40.00")


@pytest.mark.django_db
def test_saved_method_is_found_by_fingerprint(
    account, plan, subscription, fake_provider
):
    for _ in range(2):
        payment = payments.create_payment(account, plan, "monthly", provider="fake")
        payments.record_result("fake", fake_provider.paid(payment, card="tok_same"))
    assert account.saved_payment_methods.count() == 1
    assert account.saved_payment_methods.get().is_default


@pytest.mark.django_db
def test_concurrent_create_with_same_reference_returns_existing(
    account, plan, subscription, fake_provider
):
    first = payments.create_payment(
        account,
        plan,
        "monthly",
        provider="fake",
        purpose="renewal",
        reference="renew_x",
    )
    second = payments.create_payment(
        account,
        plan,
        "monthly",
        provider="fake",
        purpose="renewal",
        reference="renew_x",
    )
    assert first.pk == second.pk

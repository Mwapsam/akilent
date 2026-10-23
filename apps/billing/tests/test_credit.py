from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing import credit, payments
from apps.billing.models import (
    BillingPayment,
    CreditGrant,
    CreditTransaction,
    Subscription,
)

D = Decimal


@pytest.mark.django_db
def test_grant_and_balance(account):
    credit.grant(account, "150", reason=CreditGrant.EARLY_ADOPTER)
    credit.grant(account, "10.50")
    assert credit.balance(account) == D("160.50")


@pytest.mark.django_db
@pytest.mark.parametrize("amount", ["0", "-5", "abc"])
def test_grant_rejects_bad_amounts(account, amount):
    with pytest.raises(credit.CreditError):
        credit.grant(account, amount)


@pytest.mark.django_db
def test_expired_and_revoked_credit_is_not_counted(account):
    soon = credit.grant(account, "20", expires_at=timezone.now() + timedelta(days=1))
    other = credit.grant(account, "30")
    credit.revoke(other)
    CreditGrant.objects.filter(pk=soon.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    assert credit.balance(account) == 0
    assert credit.expire_due() == 1
    assert CreditTransaction.objects.filter(kind=CreditTransaction.EXPIRED).count() == 1


@pytest.mark.django_db
def test_apply_uses_earliest_expiring_first(account, plan, subscription, fake_provider):
    never = credit.grant(account, "10")
    soon = credit.grant(account, "10", expires_at=timezone.now() + timedelta(days=5))
    payment = payments.create_payment(account, plan, "monthly", provider="fake")

    never.refresh_from_db()
    soon.refresh_from_db()
    assert soon.remaining == 0  # used first
    assert never.remaining == D("1.00")  # $19 plan: 10 + 9
    assert payment.credit_applied == D("19.00")


@pytest.mark.django_db
def test_partial_credit_lowers_amount_due(account, plan, subscription, fake_provider):
    credit.grant(account, "5")
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    assert payment.credit_applied == D("5.00")
    assert payment.amount_due == D("14.00")
    assert payment.status == BillingPayment.PENDING


@pytest.mark.django_db
def test_credit_covering_everything_activates_without_a_provider(
    account, plan, subscription, fake_provider
):
    credit.grant(account, "150", reason=CreditGrant.EARLY_ADOPTER)
    payment = payments.create_payment(account, plan, "quarterly", provider="fake")

    subscription.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED and payment.provider == "credit"
    assert subscription.status == Subscription.ACTIVE
    assert subscription.billing_period == "quarterly"
    assert credit.balance(account) == D("150") - payment.gross_amount
    assert not fake_provider.charges


@pytest.mark.django_db
def test_abandoned_checkout_returns_credit_and_late_payment_reclaims_it(
    account, plan, subscription, fake_provider
):
    credit.grant(account, "5")
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.abandon_open_checkouts(account)
    assert credit.balance(account) == D("5.00")

    # The customer finishes the old checkout anyway: honoured, and the credit is taken again.
    payments.record_result("fake", fake_provider.paid(payment))
    payment.refresh_from_db()
    subscription.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    assert subscription.status == Subscription.ACTIVE
    assert credit.balance(account) == 0


@pytest.mark.django_db
def test_restore_is_idempotent(account, plan, subscription, fake_provider):
    credit.grant(account, "5")
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    assert credit.restore(payment) == D("5.00")
    assert credit.restore(payment) == 0
    assert credit.balance(account) == D("5.00")


@pytest.mark.django_db
def test_early_adopter_bulk_grant_is_once_per_business(account):
    from apps.accounts.models import PilotEnrollment

    PilotEnrollment.objects.create(account=account, wave=1)
    granted, _ = credit.grant_early_adopters("150")
    again, skipped = credit.grant_early_adopters("150")
    assert granted == [account] and again == [] and skipped == [account]
    assert credit.balance(account) == D("150")

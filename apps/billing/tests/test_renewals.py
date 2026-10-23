from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing import credit, payments, renewals
from apps.billing.models import BillingPayment, SavedPaymentMethod, Subscription
from apps.billing.providers import FAILED, PENDING, REQUIRES_ACTION, SavedMethodData

D = Decimal


@pytest.fixture
def due(account, plan, subscription):
    """An active monthly subscription whose period just ended."""
    now = timezone.now()
    subscription.status = Subscription.ACTIVE
    subscription.provider = "fake"
    subscription.current_period_start = now - timedelta(days=30)
    subscription.current_period_end = now - timedelta(minutes=5)
    subscription.save()
    return subscription


def _card(account, provider="fake"):
    return payments.save_method(
        account, provider, SavedMethodData(token="tok_1", last4="4242")
    )


@pytest.mark.django_db
def test_credit_renews_without_charging(account, due, fake_provider):
    credit.grant(account, "50")
    _card(account)
    counts = renewals.run()

    due.refresh_from_db()
    assert counts["renewed"] == 1
    assert due.status == Subscription.ACTIVE and due.current_period_end > timezone.now()
    assert due.provider == "credit"
    assert not fake_provider.charges
    assert credit.balance(account) == D("31.00")


@pytest.mark.django_db
def test_saved_card_is_charged_for_what_credit_doesnt_cover(
    account, due, fake_provider
):
    credit.grant(account, "4")
    _card(account)
    renewals.run()

    due.refresh_from_db()
    assert fake_provider.charges == [("tok_1", fake_provider.charges[0][1], D("15.00"))]
    assert due.status == Subscription.ACTIVE and due.current_period_end > timezone.now()


@pytest.mark.django_db
def test_declined_card_goes_past_due_and_returns_credit(account, due, fake_provider):
    credit.grant(account, "4")
    _card(account)
    fake_provider.next_status = FAILED
    renewals.run()

    due.refresh_from_db()
    assert due.status == Subscription.PAST_DUE and due.past_due_since
    assert credit.balance(account) == D("4.00")


@pytest.mark.django_db
def test_requires_action_goes_past_due(account, due, fake_provider):
    _card(account)
    fake_provider.next_status = REQUIRES_ACTION
    renewals.run()
    due.refresh_from_db()
    assert due.status == Subscription.PAST_DUE


@pytest.mark.django_db
def test_overlapping_runs_charge_once(account, due, fake_provider):
    _card(account)
    fake_provider.next_status = PENDING  # nothing settles between the two runs
    renewals.renew(due.pk)
    renewals.renew(due.pk)
    assert len(fake_provider.charges) == 1


@pytest.mark.django_db
def test_unconfirmed_renewal_charge_eventually_goes_past_due(
    account, due, fake_provider
):
    _card(account)
    fake_provider.next_status = PENDING
    renewals.run()
    BillingPayment.objects.update(updated_at=timezone.now() - timedelta(days=2))
    payments.abandon_stale()

    due.refresh_from_db()
    assert due.status == Subscription.PAST_DUE


@pytest.mark.django_db
def test_card_saved_with_old_provider_is_not_used_after_a_switch(
    account, due, fake_provider
):
    _card(account, provider="stripe")  # saved before the operator switched to "fake"
    renewals.run()

    due.refresh_from_db()
    assert not fake_provider.charges
    assert (
        due.status == Subscription.PAST_DUE
    )  # emailed a link to pay with the new provider


@pytest.mark.django_db
def test_past_due_expires_after_grace(account, due, fake_provider, settings):
    settings.BILLING_GRACE_DAYS = 7
    renewals.run()
    due.refresh_from_db()
    assert due.status == Subscription.PAST_DUE

    renewals.run(now=due.past_due_since + timedelta(days=7, minutes=1))
    due.refresh_from_db()
    assert due.status == Subscription.EXPIRED


@pytest.mark.django_db
def test_paying_the_link_after_past_due_reactivates(account, due, plan, fake_provider):
    renewals.run()
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.record_result("fake", fake_provider.paid(payment))

    due.refresh_from_db()
    assert due.status == Subscription.ACTIVE and due.past_due_since is None
    assert SavedPaymentMethod.objects.filter(account=account, provider="fake").exists()


@pytest.mark.django_db
def test_legacy_provider_subscriptions_are_left_alone(account, due, fake_provider):
    due.stripe_subscription_id = "sub_legacy"
    due.save()
    renewals.run()
    due.refresh_from_db()
    assert due.status == Subscription.ACTIVE and not fake_provider.charges


@pytest.mark.django_db
def test_upcoming_notice_sent_once(account, due, fake_provider):
    due.current_period_end = timezone.now() + timedelta(days=2)
    due.save()
    renewals.send_due_notices()
    renewals.send_due_notices()
    due.refresh_from_db()
    assert due.renewal_notice_for == due.current_period_end

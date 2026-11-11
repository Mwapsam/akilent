"""A business that signed up for a paid plan and didn't finish paying is reminded twice, one
and three days after signup, with a link back to its payment page. Never while a payment may
already be on its way, never when there's no way to pay, and never for an old signup."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.billing import credit, payments, renewals, tasks
from apps.billing.models import BillingPayment, ManualPaymentRequest, Subscription
from apps.core.models import SiteSettings

NOTICE = "apps.billing.tasks.send_billing_notice.delay"


def _kinds(mock):
    return [c.args[1] for c in mock.call_args_list]


@pytest.fixture
def unpaid(account, plan, fake_provider):
    site = SiteSettings.load()
    site.payments_enabled = True
    site.save()
    sub = Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.INCOMPLETE,
        billing_period=Subscription.MONTHLY,
        current_period_start=timezone.now(),
    )
    return sub


def _signed_up_ago(sub, delta):
    Subscription.objects.filter(pk=sub.pk).update(created_at=timezone.now() - delta)
    sub.refresh_from_db()


def _run(now=None, capture=None):
    with patch(NOTICE) as notice:
        with capture(execute=True):
            renewals.send_finish_payment_reminders(now)
    return _kinds(notice)


@pytest.mark.django_db
def test_reminded_on_day_one_and_day_three_only(
    unpaid, django_capture_on_commit_callbacks
):
    cap = django_capture_on_commit_callbacks
    _signed_up_ago(unpaid, timedelta(hours=20))
    assert _run(capture=cap) == []

    _signed_up_ago(unpaid, timedelta(days=1, minutes=1))
    assert _run(capture=cap) == ["finish_payment"]
    assert _run(capture=cap) == []  # the next hourly run sends nothing more

    _signed_up_ago(unpaid, timedelta(days=3, minutes=1))
    assert _run(capture=cap) == ["finish_payment"]

    _signed_up_ago(unpaid, timedelta(days=5))
    assert _run(capture=cap) == []
    unpaid.refresh_from_db()
    assert unpaid.dunning_notices_sent == 2


@pytest.mark.django_db
def test_the_hourly_notice_run_sends_it(unpaid, django_capture_on_commit_callbacks):
    _signed_up_ago(unpaid, timedelta(days=1, minutes=1))
    with patch(NOTICE) as notice:
        with django_capture_on_commit_callbacks(execute=True):
            renewals.send_due_notices()
    assert _kinds(notice) == ["finish_payment"]


@pytest.mark.django_db
def test_an_old_signup_is_not_chased(unpaid, django_capture_on_commit_callbacks):
    _signed_up_ago(unpaid, timedelta(days=8))
    assert _run(capture=django_capture_on_commit_callbacks) == []


@pytest.mark.django_db
def test_no_reminder_while_a_payment_is_in_flight(
    account, plan, unpaid, django_capture_on_commit_callbacks
):
    _signed_up_ago(unpaid, timedelta(days=1, minutes=1))
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.mark_charge_started(payment)
    assert _run(capture=django_capture_on_commit_callbacks) == []


@pytest.mark.django_db
def test_no_reminder_while_a_bank_transfer_is_waiting(
    account, plan, unpaid, django_capture_on_commit_callbacks
):
    _signed_up_ago(unpaid, timedelta(days=1, minutes=1))
    ManualPaymentRequest.objects.create(
        account=account, plan=plan, billing_period="monthly", reference="BANK-1"
    )
    assert _run(capture=django_capture_on_commit_callbacks) == []


@pytest.mark.django_db
def test_no_reminder_when_payments_are_switched_off(
    unpaid, django_capture_on_commit_callbacks
):
    _signed_up_ago(unpaid, timedelta(days=1, minutes=1))
    site = SiteSettings.load()
    site.payments_enabled = False
    site.save()
    assert _run(capture=django_capture_on_commit_callbacks) == []


@pytest.mark.django_db
def test_no_reminder_once_paid(
    account, plan, unpaid, fake_provider, django_capture_on_commit_callbacks
):
    _signed_up_ago(unpaid, timedelta(days=1, minutes=1))
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.record_result("fake", fake_provider.paid(payment))
    assert BillingPayment.objects.get(pk=payment.pk).status == BillingPayment.SUCCEEDED
    assert _run(capture=django_capture_on_commit_callbacks) == []


@pytest.mark.django_db
def test_the_email_quotes_what_checkout_charges_and_links_to_pay(account, unpaid):
    credit.grant(account, "4")
    subject, body, html = tasks.notice_email(unpaid, "finish_payment", None)
    assert subject == "Finish setting up your Starter plan"
    assert "$15.00" in body and "$4.00" in body  # $19 less $4 credit
    assert "/billing/pay/" in body and "/billing/pay/" in html

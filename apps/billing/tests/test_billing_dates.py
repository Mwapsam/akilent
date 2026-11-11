"""Every billing date is acted on, on time, and the customer is told about it truthfully:
trials lock at their end whatever the jobs do, renewals never creep later, pay-link customers
can pay before the date, and a missed date is reported to operators."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Membership
from apps.billing import api as billing_api
from apps.billing import credit, health, payments, renewals, tasks
from apps.billing.models import BillingPayment, Subscription
from apps.billing.providers import (
    FAILED,
    PENDING,
    ProviderResult,
    SavedMethodData,
    registry,
)
from apps.core.console import business as console
from apps.core.console.business import ConsoleError
from apps.core.models import SiteSettings

from .conftest import FakeProvider

NOTICE = "apps.billing.tasks.send_billing_notice.delay"


def _kinds(mock):
    return [c.args[1] for c in mock.call_args_list]


@pytest.fixture
def due(subscription):
    """An active monthly subscription whose period ended five minutes ago."""
    now = timezone.now()
    subscription.status = Subscription.ACTIVE
    subscription.provider = "fake"
    subscription.current_period_start = now - timedelta(days=30)
    subscription.current_period_end = now - timedelta(minutes=5)
    subscription.save()
    return subscription


@pytest.fixture
def trial(subscription):
    subscription.status = Subscription.TRIALING
    subscription.trial_ends_at = timezone.now() + timedelta(days=2)
    subscription.save()
    return subscription


def _card(account):
    return payments.save_method(
        account, "fake", SavedMethodData(token="tok_1", last4="4242")
    )


# --- trials ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_trial_locks_at_its_end_even_if_the_job_never_runs(account, trial):
    assert billing_api._read_state(account).plan_keys
    trial.trial_ends_at = timezone.now() - timedelta(minutes=1)
    trial.save()
    assert trial.status == Subscription.TRIALING  # expire_trials hasn't run
    assert not billing_api._read_state(account).plan_keys


@pytest.mark.django_db
def test_past_due_locks_when_grace_ends_even_if_the_job_never_runs(
    account, due, settings
):
    settings.BILLING_GRACE_DAYS = 7
    due.status = Subscription.PAST_DUE
    due.past_due_since = timezone.now() - timedelta(days=6)
    due.save()
    assert billing_api._read_state(account).plan_keys
    due.past_due_since = timezone.now() - timedelta(days=7, minutes=1)
    due.save()
    assert not billing_api._read_state(account).plan_keys


@pytest.mark.django_db
def test_trial_ending_email_is_sent_once(
    account, trial, django_capture_on_commit_callbacks
):
    with patch(NOTICE) as notice:
        with django_capture_on_commit_callbacks(execute=True):
            renewals.send_due_notices()
            renewals.send_due_notices()
    assert _kinds(notice) == ["trial_ending"]


@pytest.mark.django_db
def test_an_ended_trial_is_expired_and_the_owner_told(account, trial):
    trial.trial_ends_at = timezone.now() - timedelta(minutes=1)
    trial.save()
    with patch(NOTICE) as notice:
        assert tasks.expire_trials() == 1
        assert tasks.expire_trials() == 0
    trial.refresh_from_db()
    assert trial.status == Subscription.EXPIRED
    assert _kinds(notice) == ["trial_ended"]


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["trial_ending", "trial_ended"])
def test_trial_emails_have_text(account, trial, kind):
    subject, body, html = tasks.notice_email(trial, kind, None)
    assert "trial" in subject.lower() and "/billing/plans/" in body and html


# --- renewal dates --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_credit_renewal_starts_where_the_last_period_ended(
    account, due, fake_provider
):
    old_end = due.current_period_end
    credit.grant(account, "50")
    renewals.run()
    due.refresh_from_db()
    assert due.current_period_start == old_end
    assert due.current_period_end == old_end + timedelta(days=30)


@pytest.mark.django_db
def test_a_charged_renewal_starts_where_the_last_period_ended(
    account, due, fake_provider
):
    old_end = due.current_period_end
    _card(account)
    renewals.run()
    due.refresh_from_db()
    assert due.current_period_end == old_end + timedelta(days=30)


@pytest.mark.django_db
def test_paying_by_link_after_past_due_starts_a_fresh_period(
    account, due, plan, fake_provider
):
    old_end = due.current_period_end
    renewals.run()
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.record_result("fake", fake_provider.paid(payment))
    due.refresh_from_db()
    assert due.status == Subscription.ACTIVE
    assert due.current_period_start > old_end


# --- what the customer is told --------------------------------------------------------------------


@pytest.mark.django_db
def test_a_pay_link_customer_is_told_the_renewal_is_due_not_that_it_failed(
    account, due, fake_provider, django_capture_on_commit_callbacks
):
    with patch(NOTICE) as notice:
        with django_capture_on_commit_callbacks(execute=True):
            renewals.run()
    assert _kinds(notice) == ["due"]
    subject, body, _ = tasks.notice_email(
        Subscription.objects.get(pk=due.pk), "due", None
    )
    assert "due" in subject and "couldn't" not in body
    assert "/billing/checkout/?plan=starter" in body


@pytest.mark.django_db
def test_a_declined_card_is_told_the_payment_failed(
    account, due, fake_provider, django_capture_on_commit_callbacks
):
    _card(account)
    fake_provider.next_status = FAILED
    with patch(NOTICE) as notice:
        with django_capture_on_commit_callbacks(execute=True):
            renewals.run()
    assert _kinds(notice) == ["unpaid"]


@pytest.mark.django_db
def test_reminders_keep_the_due_wording_for_a_pay_link_customer(
    account, due, fake_provider, django_capture_on_commit_callbacks
):
    renewals.run()
    due.refresh_from_db()
    with patch(NOTICE) as notice:
        with django_capture_on_commit_callbacks(execute=True):
            renewals.send_due_notices(due.past_due_since + timedelta(days=3, minutes=1))
    assert _kinds(notice) == ["due_reminder"]


@pytest.mark.django_db
def test_upcoming_email_has_a_pay_link_only_when_nothing_charges_automatically(
    account, due, fake_provider
):
    due.current_period_end = timezone.now() + timedelta(days=2)
    due.save()
    _, body, _ = tasks.notice_email(due, "upcoming", None)
    assert "/billing/checkout/" in body and "doesn't lose any days" in body

    _card(account)
    _, body, _ = tasks.notice_email(due, "upcoming", None)
    assert "/billing/checkout/" not in body and "charged automatically" in body


@pytest.mark.django_db
def test_past_due_status_gives_the_pay_by_date(account, due, settings):
    settings.BILLING_GRACE_DAYS = 7
    now = timezone.now()
    due.status = Subscription.PAST_DUE
    due.past_due_since = now - timedelta(days=1)
    due.save()
    status = billing_api.plan_status(due, now)
    assert status["label"] == "Payment due"
    keep = timezone.localtime(due.past_due_since + timedelta(days=7))
    assert f"Pay by {keep.day} {keep:%b}" in status["line"]
    assert "Pay by" in billing_api.home_notice(due, now)["text"]


@pytest.mark.django_db
def test_a_paypal_renewal_waiting_for_its_charge_is_not_a_warning(account, due):
    due.recurring_provider, due.recurring_id = "paypal", "I-123"
    due.save()
    now = timezone.now()
    assert billing_api.plan_status(due, now)["tone"] == "info"
    assert billing_api.home_notice(due, now) is None
    later = due.current_period_end + renewals.RECURRING_GRACE + timedelta(hours=1)
    assert billing_api.plan_status(due, later)["label"] == "Renewal pending"


class _Reviewing(FakeProvider):
    code = "reviewing"
    can_reconcile = True

    def reconcile(self, payment):
        return ProviderResult(reference=payment.reference, status=PENDING)


@pytest.mark.django_db
def test_a_held_first_payment_sends_one_review_email(
    account, plan, subscription, django_capture_on_commit_callbacks
):
    provider = _Reviewing()
    registry.register(provider)
    try:
        payment = payments.create_payment(
            account, plan, "monthly", provider="reviewing"
        )
        payments.mark_charge_started(payment)
        BillingPayment.objects.filter(pk=payment.pk).update(
            charge_started_at=timezone.now() - timedelta(hours=1)
        )
        with patch(NOTICE) as notice:
            with django_capture_on_commit_callbacks(execute=True):
                payments.abandon_stale()
                payments.abandon_stale()
    finally:
        registry.unregister(provider.code)
    assert _kinds(notice) == ["payment_review"]
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING


# --- operator console -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_setting_active_after_past_due_gives_the_next_renewal_a_full_grace(
    account, due, plan
):
    due.status = Subscription.PAST_DUE
    due.past_due_since = timezone.now() - timedelta(days=20)
    due.dunning_notices_sent = 2
    due.save()
    console.set_subscription(account, plan.pk, Subscription.ACTIVE)
    due.refresh_from_db()
    assert due.past_due_since is None and due.dunning_notices_sent == 0


@pytest.mark.django_db
def test_setting_past_due_by_hand_starts_the_grace_period(account, due, plan):
    console.set_subscription(account, plan.pk, Subscription.PAST_DUE)
    due.refresh_from_db()
    assert due.past_due_since is not None


@pytest.mark.django_db
def test_a_trial_cannot_be_extended_over_a_paid_plan(account, due):
    due.current_period_end = timezone.now() + timedelta(days=10)
    due.save()
    with pytest.raises(ConsoleError):
        console.extend_trial(account, 7)
    due.refresh_from_db()
    assert due.status == Subscription.ACTIVE


@pytest.mark.django_db
def test_extending_a_trial_resends_the_trial_ending_email(account, trial):
    trial.renewal_notice_for = trial.trial_ends_at
    trial.save()
    console.extend_trial(account, 7)
    trial.refresh_from_db()
    assert trial.renewal_notice_for is None


# --- renewing a hidden plan -----------------------------------------------------------------------


@pytest.mark.django_db
def test_a_customer_can_renew_a_plan_that_is_no_longer_sold(
    client, account, due, plan, fake_provider
):
    user = User.objects.create_user("renewer", "renewer@example.com", "pw12345!")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    site = SiteSettings.load()
    site.payments_enabled = True
    site.save()
    plan.is_active = False
    plan.save()
    client.force_login(user)
    resp = client.get("/billing/checkout/?plan=starter&period=monthly")
    assert resp.status_code == 302 and resp["Location"].startswith(
        "https://pay.example/"
    )

    Subscription.objects.filter(pk=due.pk).delete()
    assert (
        client.get("/billing/checkout/?plan=starter&period=monthly").status_code == 404
    )


# --- health check ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_health_check_is_quiet_when_all_is_well(account, due, fake_provider):
    renewals.run()
    assert health.problems() == []


@pytest.mark.django_db
def test_health_check_reports_a_missed_renewal_and_trial(account, due):
    due.current_period_end = timezone.now() - timedelta(hours=5)
    due.save()
    found = health.problems()
    assert any(line.startswith("Renewal missed") for line in found)

    due.status = Subscription.TRIALING
    due.trial_ends_at = timezone.now() - timedelta(hours=5)
    due.save()
    assert any(line.startswith("Trial not ended") for line in health.problems())


@pytest.mark.django_db
def test_health_check_reports_stuck_past_due_and_unresolved(account, due, plan):
    due.status = Subscription.PAST_DUE
    due.past_due_since = None
    due.save()
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    BillingPayment.objects.filter(pk=payment.pk).update(
        resolution=BillingPayment.UNRESOLVED
    )
    found = health.problems()
    assert any("never expires" in line for line in found)
    assert any(line.startswith("Payment outcome unknown") for line in found)


@pytest.mark.django_db
def test_health_check_alerts_operators(account, due):
    User.objects.create_superuser("ops", "ops@example.com", "pw")
    due.current_period_end = timezone.now() - timedelta(hours=5)
    due.save()
    with (
        patch("apps.email.services.send.send_system_email") as email,
        patch("apps.billing.slack.post_message") as slack,
    ):
        found = tasks.billing_health_check()
    assert found
    assert email.call_args.args[0] == "ops@example.com"
    assert slack.called

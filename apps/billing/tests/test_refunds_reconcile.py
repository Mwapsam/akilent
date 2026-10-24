"""Provider-neutral pieces added for PayPal: refunds beside a paid payment, reconciling a charge
whose outcome is unknown, and saved methods scoped to a merchant account."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.billing import payments
from apps.billing.models import (
    BillingPayment,
    BillingRefund,
    SavedPaymentMethod,
    Subscription,
)
from apps.billing.providers import (
    CHARGEBACK,
    PENDING,
    REFUNDED,
    SUCCEEDED,
    ProviderError,
    ProviderResult,
    SavedMethodData,
)
from apps.core.models import AdminAction

D = Decimal


def _paid(account, plan, fake_provider):
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.record_result("fake", fake_provider.paid(payment, card=None))
    payment.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    return payment


def _refund(payment, refund_id, amount, kind="refund"):
    return ProviderResult(
        reference=payment.reference,
        status=REFUNDED,
        currency=payment.currency,
        refund_id=refund_id,
        refund_amount=D(amount),
        refund_kind=kind,
    )


# --- refunds -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_partial_then_full_refund_keeps_the_payment_paid(
    account, plan, subscription, fake_provider
):
    payment = _paid(account, plan, fake_provider)

    payments.record_result("fake", _refund(payment, "r1", "4.00"))
    payment.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    assert payment.refund_state == BillingPayment.REFUND_PARTIAL
    assert payment.refunded_amount == D("4.00")

    payments.record_result("fake", _refund(payment, "r2", "15.00"))
    payment.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    assert payment.refund_state == BillingPayment.REFUND_FULL
    assert payment.refunded_amount == D("19.00")
    assert payment.refunds.count() == 2
    assert AdminAction.objects.filter(action="billing.refund", actor=None).count() == 2


@pytest.mark.django_db
def test_replayed_refund_is_recorded_once(account, plan, subscription, fake_provider):
    payment = _paid(account, plan, fake_provider)
    payments.record_result("fake", _refund(payment, "r1", "4.00"))
    payments.record_result("fake", _refund(payment, "r1", "4.00"))
    payment.refresh_from_db()
    assert BillingRefund.objects.filter(payment=payment).count() == 1
    assert payment.refunded_amount == D("4.00")


@pytest.mark.django_db
def test_late_success_after_refund_grants_nothing_more(
    account, plan, subscription, fake_provider
):
    payment = _paid(account, plan, fake_provider)
    subscription.refresh_from_db()
    end = subscription.current_period_end

    payments.record_result("fake", _refund(payment, "r1", "19.00"))
    payments.record_result("fake", fake_provider.paid(payment, card=None))

    subscription.refresh_from_db()
    payment.refresh_from_db()
    assert subscription.current_period_end == end
    assert payment.status == BillingPayment.SUCCEEDED
    assert payment.refund_state == BillingPayment.REFUND_FULL


@pytest.mark.django_db
def test_refund_does_not_take_access_away(account, plan, subscription, fake_provider):
    _paid(account, plan, fake_provider)
    payment = BillingPayment.objects.get(account=account)
    payments.record_result("fake", _refund(payment, "cb1", "19.00", kind=CHARGEBACK))
    subscription.refresh_from_db()
    assert subscription.status == Subscription.ACTIVE
    assert AdminAction.objects.filter(action="billing.chargeback").exists()


@pytest.mark.django_db
def test_refund_without_an_id_is_rejected(account, plan, subscription, fake_provider):
    payment = _paid(account, plan, fake_provider)
    assert payments.record_result("fake", _refund(payment, "", "4.00")) is None
    assert not BillingRefund.objects.exists()


# --- reconciling a charge whose outcome is unknown ---------------------------------------------


def _stuck_renewal(account, plan, subscription, *, sent=True, reconcilable=True):
    from apps.billing.providers import registry

    registry.get("fake").can_reconcile = reconcilable
    subscription.status = Subscription.ACTIVE
    subscription.provider = "fake"
    subscription.current_period_end = timezone.now() - timedelta(days=2)
    subscription.save()
    payment = payments.create_payment(
        account,
        plan,
        "monthly",
        provider="fake",
        purpose=BillingPayment.RENEWAL,
        reference="renew_test",
    )
    old = timezone.now() - timedelta(days=2)
    BillingPayment.objects.filter(pk=payment.pk).update(
        provider_ref=payments.CHARGING,
        charge_started_at=old if sent else None,
        updated_at=old,
    )
    payment.refresh_from_db()
    return payment


@pytest.mark.django_db
def test_unknown_outcome_is_held_not_failed(account, plan, subscription, fake_provider):
    payment = _stuck_renewal(account, plan, subscription)

    payments.abandon_stale()

    payment.refresh_from_db()
    subscription.refresh_from_db()
    assert payment.status == BillingPayment.PENDING
    assert payment.resolution == BillingPayment.UNRESOLVED
    # Not sent to dunning: the pay link couldn't be used, and the charge may have gone through.
    assert subscription.status == Subscription.ACTIVE
    assert subscription.past_due_since is None
    assert AdminAction.objects.filter(action="billing.unresolved").count() == 1
    assert fake_provider.charges == []  # never charged again

    payments.abandon_stale()  # a later pass doesn't re-alert
    assert AdminAction.objects.filter(action="billing.unresolved").count() == 1


@pytest.mark.django_db
def test_unresolved_payment_blocks_a_new_checkout(
    account, plan, subscription, fake_provider
):
    _stuck_renewal(account, plan, subscription)
    payments.abandon_stale()
    with pytest.raises(payments.UnresolvedPayment):
        payments.create_payment(account, plan, "monthly", provider="fake")


@pytest.mark.django_db
def test_checkout_view_explains_the_block(
    client, account, plan, subscription, fake_provider, django_user_model
):
    from apps.accounts.models import Membership
    from apps.core.models import SiteSettings

    site = SiteSettings.load()
    site.payments_enabled = True
    site.save()
    user = django_user_model.objects.create_user("owner", "o@example.com", "pw")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    _stuck_renewal(account, plan, subscription)
    payments.abandon_stale()

    response = client.get("/billing/checkout/?plan=starter&period=monthly")

    assert response.status_code == 302
    assert response["Location"] == "/billing/plans/"
    assert BillingPayment.objects.filter(purpose=BillingPayment.CHECKOUT).count() == 0


@pytest.mark.django_db
def test_reconcile_settles_a_charge_that_went_through(
    account, plan, subscription, fake_provider
):
    payment = _stuck_renewal(account, plan, subscription)
    fake_provider.reconcile = lambda p: ProviderResult(
        reference=p.reference,
        status=SUCCEEDED,
        amount=p.amount_due,
        currency=p.currency,
    )

    payments.abandon_stale()

    payment.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    assert payment.resolution == ""


@pytest.mark.django_db
def test_reconcile_error_is_treated_as_unknown(
    account, plan, subscription, fake_provider
):
    payment = _stuck_renewal(account, plan, subscription)

    def boom(p):
        raise ProviderError("timeout")

    fake_provider.reconcile = boom
    payments.abandon_stale()
    payment.refresh_from_db()
    assert payment.resolution == BillingPayment.UNRESOLVED
    assert payment.status == BillingPayment.PENDING


@pytest.mark.django_db
def test_still_pending_at_the_provider_keeps_waiting(
    account, plan, subscription, fake_provider
):
    """e.g. a capture under PayPal review: a known state, not an operator problem."""
    payment = _stuck_renewal(account, plan, subscription)
    fake_provider.reconcile = lambda p: ProviderResult(
        reference=p.reference, status=PENDING
    )
    payments.abandon_stale()
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING
    assert payment.resolution == ""
    assert not AdminAction.objects.filter(action="billing.unresolved").exists()
    assert payments.has_unconfirmed_charge(account)  # still can't pay twice


@pytest.mark.django_db
def test_late_confirmation_resolves_and_unblocks(
    account, plan, subscription, fake_provider
):
    payment = _stuck_renewal(account, plan, subscription)
    payments.abandon_stale()

    payments.record_result("fake", fake_provider.paid(payment, card=None))

    payment.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    assert payment.resolution == ""
    assert not payments.has_unconfirmed_charge(account)
    assert AdminAction.objects.filter(action="billing.resolved").exists()


@pytest.mark.django_db
def test_charge_never_sent_still_fails_as_before(
    account, plan, subscription, fake_provider
):
    payment = _stuck_renewal(account, plan, subscription, sent=False)
    payments.abandon_stale()
    payment.refresh_from_db()
    assert payment.status == BillingPayment.FAILED
    assert payment.resolution == ""


# --- saved methods scoped to a merchant account ------------------------------------------------


@pytest.mark.django_db
def test_method_from_another_merchant_account_is_never_charged(account, fake_provider):
    fake_provider.account_key = lambda: "live:A"
    payments.save_method(account, "fake", SavedMethodData(token="tok_a"))
    method = SavedPaymentMethod.objects.get(account=account)
    assert method.provider_account == "live:A"  # filled in from the provider

    fake_provider.account_key = lambda: "live:B"
    assert payments.default_method(account, "fake") is None
    payments.save_method(account, "fake", SavedMethodData(token="tok_b"))
    assert payments.default_method(account, "fake").token == "tok_b"


@pytest.mark.django_db
def test_blank_merchant_key_keeps_existing_methods_usable(account, fake_provider):
    SavedPaymentMethod.objects.create(
        account=account,
        provider="fake",
        token="tok_old",
        token_fingerprint=payments.token_fingerprint("tok_old"),
    )
    assert payments.default_method(account, "fake").token == "tok_old"


@pytest.mark.django_db
def test_label_is_kept_apart_from_last4(account, fake_provider):
    payments.save_method(
        account,
        "fake",
        SavedMethodData(token="tok_pp", brand="PayPal", label="PayPal · j***@x.com"),
    )
    method = SavedPaymentMethod.objects.get(account=account)
    assert method.label == "PayPal · j***@x.com"
    assert method.last4 == ""


# --- review fixes ------------------------------------------------------------------------------


def _unresolved_checkout(account, plan, fake_provider):
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.mark_charge_started(payment)
    payments.mark_unresolved(payment)
    payment.refresh_from_db()
    return payment


@pytest.mark.django_db
def test_starting_another_checkout_keeps_an_unresolved_one(
    account, plan, subscription, fake_provider
):
    """The guard used to be cleared: abandon_open_checkouts ran first and abandoned it."""
    payment = _unresolved_checkout(account, plan, fake_provider)
    payments.abandon_open_checkouts(account)
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING
    assert payment.resolution == BillingPayment.UNRESOLVED
    with pytest.raises(payments.UnresolvedPayment):
        payments.create_payment(account, plan, "monthly", provider="fake")


@pytest.mark.django_db
def test_stale_unresolved_checkout_is_not_abandoned(
    account, plan, subscription, fake_provider
):
    payment = _unresolved_checkout(account, plan, fake_provider)
    BillingPayment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(days=2)
    )
    payments.abandon_stale()
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING


@pytest.mark.django_db
def test_stale_checkout_whose_capture_was_sent_is_reconciled(
    account, plan, subscription, fake_provider
):
    fake_provider.can_reconcile = True
    fake_provider.reconcile = lambda p: ProviderResult(
        reference=p.reference,
        status=SUCCEEDED,
        amount=p.amount_due,
        currency=p.currency,
    )
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.mark_charge_started(payment)
    BillingPayment.objects.filter(pk=payment.pk).update(
        created_at=timezone.now() - timedelta(days=2)
    )
    payments.abandon_stale()
    payment.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED


@pytest.mark.django_db
def test_claiming_a_renewal_charge_marks_it_sent(
    account, plan, subscription, fake_provider
):
    payment = payments.create_payment(
        account,
        plan,
        "monthly",
        provider="fake",
        purpose=BillingPayment.RENEWAL,
        reference="renew_claim",
    )
    assert payments.claim_for_charge(payment)
    payment.refresh_from_db()
    assert payment.charge_started_at is not None


@pytest.mark.django_db
def test_provider_that_cant_reconcile_gives_up_as_before(
    account, plan, subscription, fake_provider
):
    payment = _stuck_renewal(account, plan, subscription, reconcilable=False)
    payments.abandon_stale()
    payment.refresh_from_db()
    assert payment.status == BillingPayment.FAILED
    assert payment.resolution == ""


@pytest.mark.django_db
def test_repeated_reconcile_passes_have_no_side_effects(
    account, plan, subscription, fake_provider
):
    payment = _stuck_renewal(account, plan, subscription)
    payments.abandon_stale()
    subscription.refresh_from_db()
    payment.refresh_from_db()
    since, touched = subscription.past_due_since, payment.updated_at

    payments.abandon_stale()  # looked up again straight away, not after another day

    subscription.refresh_from_db()
    payment.refresh_from_db()
    assert subscription.past_due_since == since
    assert payment.status == BillingPayment.PENDING
    assert payment.updated_at == touched  # not re-saved
    assert AdminAction.objects.filter(action="billing.unresolved").count() == 1


@pytest.mark.django_db
def test_refund_before_the_payment_is_confirmed_counts_once_it_is(
    account, plan, subscription, fake_provider
):
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.record_result("fake", _refund(payment, "r1", "5.00"))
    payment.refresh_from_db()
    assert payment.refunds.count() == 1
    assert payment.refund_state == BillingPayment.REFUND_NONE
    assert payment.refunded_amount == D("0")

    payments.record_result("fake", fake_provider.paid(payment, card=None))
    payment.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    assert payment.refund_state == BillingPayment.REFUND_PARTIAL
    assert payment.refunded_amount == D("5.00")


@pytest.mark.django_db
def test_refund_in_another_currency_is_not_counted(
    account, plan, subscription, fake_provider
):
    payment = _paid(account, plan, fake_provider)
    result = _refund(payment, "r1", "5.00")
    result.currency = "EUR"
    payments.record_result("fake", result)
    payment.refresh_from_db()
    assert payment.refunds.count() == 1
    assert payment.refund_state == BillingPayment.REFUND_NONE
    assert payment.refunded_amount == D("0")


# --- review round 2 ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_in_flight_checkout_is_kept_and_blocks_another(
    account, plan, subscription, fake_provider
):
    """A capture was sent, the outcome isn't known yet (not yet UNRESOLVED)."""
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.mark_charge_started(payment)
    payments.abandon_open_checkouts(account)
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING
    with pytest.raises(payments.UnresolvedPayment):
        payments.create_payment(account, plan, "monthly", provider="fake")


@pytest.mark.django_db
def test_refused_charge_is_an_ordinary_checkout_again(
    account, plan, subscription, fake_provider
):
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    payments.mark_charge_started(payment)
    payments.clear_charge_started(payment)
    payments.abandon_open_checkouts(account)
    payment.refresh_from_db()
    assert payment.status == BillingPayment.ABANDONED


@pytest.mark.django_db
def test_unresolved_payment_is_looked_up_on_every_run(
    account, plan, subscription, fake_provider
):
    calls = []
    payment = _stuck_renewal(account, plan, subscription)
    fake_provider.reconcile = lambda p: calls.append(p.pk)
    payments.abandon_stale()
    payments.abandon_stale()
    payments.abandon_stale()
    assert calls == [payment.pk] * 3


@pytest.mark.django_db
def test_credit_is_taken_again_when_a_payment_needing_action_succeeds(
    account, plan, subscription, fake_provider
):
    from apps.billing import credit
    from apps.billing.providers import REQUIRES_ACTION

    credit.grant(account, "5.00")
    payment = payments.create_payment(account, plan, "monthly", provider="fake")
    assert payment.credit_applied == D("5.00")
    assert credit.balance(account) == D("0.00")

    payments.record_result(
        "fake",
        ProviderResult(
            reference=payment.reference, status=REQUIRES_ACTION, action_url="https://x"
        ),
    )
    assert credit.balance(account) == D("5.00")  # given back while it waits

    payments.record_result("fake", fake_provider.paid(payment, card=None))
    payment.refresh_from_db()
    assert payment.status == BillingPayment.SUCCEEDED
    assert credit.balance(account) == D("0.00")  # paid the reduced amount, so it's used


@pytest.mark.django_db
def test_audit_failure_doesnt_break_the_callers_transaction(account):
    from django.db import transaction

    from apps.core import audit

    with transaction.atomic():
        audit.system("billing.refund", account=account, target="x", bad=object())
        assert AdminAction.objects.count() == 0  # the transaction is still usable


@pytest.mark.django_db
def test_early_recheck_never_fails_a_charge_for_providers_that_cant_reconcile(
    account, plan, subscription, fake_provider
):
    """Stripe/Flutterwave keep their 24h window even though the charge is in flight."""
    payment = payments.create_payment(
        account,
        plan,
        "monthly",
        provider="fake",
        purpose=BillingPayment.RENEWAL,
        reference="renew_early",
    )
    payments.claim_for_charge(payment)
    BillingPayment.objects.filter(pk=payment.pk).update(
        charge_started_at=timezone.now() - timedelta(hours=1)
    )
    payments.abandon_stale()
    payment.refresh_from_db()
    assert payment.status == BillingPayment.PENDING


# --- migration ---------------------------------------------------------------------------------


def test_0028_only_adds_and_is_reversible():
    """Tests run with --nomigrations, so this checks the migration's shape: only new tables and
    new columns that existing rows can take without a backfill, so rolling back to 0027 drops
    them and nothing else."""
    import importlib

    from django.db import migrations

    migration = importlib.import_module("apps.billing.migrations.0028_paypal_support")
    for op in migration.Migration.operations:
        assert isinstance(op, (migrations.AddField, migrations.CreateModel)), op
        if isinstance(op, migrations.AddField):
            field = op.field
            assert field.null or field.has_default(), op.name

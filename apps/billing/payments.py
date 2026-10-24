"""The provider-neutral payment flow: create a payment for a plan period (credit first), and turn
what a provider reports into subscription state.

Every provider goes through ``record_result``; it is the only code that marks a payment paid or
failed, and it does so once, under a row lock, so a checkout return racing its webhook (or a
replayed event) can never grant a period twice.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import credit
from .models import (
    BillingPayment,
    BillingRefund,
    CreditGrant,
    SavedPaymentMethod,
    Subscription,
)
from .money import billing_currency, covers
from .pricing import price_for
from .providers import (
    FAILED,
    PENDING,
    REFUNDED,
    REQUIRES_ACTION,
    SUCCEEDED,
    ProviderResult,
)
from .services import activate_subscription, unused_value

logger = logging.getLogger(__name__)

CREDIT = "credit"
# provider_ref of a renewal whose saved-method charge is in flight (see claim_for_charge).
CHARGING = "charging"
ZERO = Decimal("0.00")
# A checkout nobody finished gives its credit back after this long.
ABANDON_AFTER = timedelta(hours=24)
# A charge in flight (sent, outcome not heard) is looked up this soon with a provider that can
# reconcile, so a lost answer doesn't block the customer for a day.
RECHECK_IN_FLIGHT_AFTER = timedelta(minutes=15)
# Still pending at the provider (e.g. a PayPal review) this long after it was sent: an operator
# is told, and it keeps being looked up.
ALERT_PENDING_AFTER = timedelta(days=3)


class UnresolvedPayment(Exception):
    """The account has a charge whose outcome isn't known yet; another payment for the same
    period could charge the customer twice."""


def create_payment(
    account,
    plan,
    billing_period: str,
    *,
    provider: str,
    purpose: str = BillingPayment.CHECKOUT,
    reference: str | None = None,
) -> BillingPayment:
    """A payment for one ``billing_period`` of ``plan``, with the account's credit already used.

    Moving to a different plan or period mid-way takes the unused paid time off the price first
    (``services.unused_value``), then credit. When those cover all of it the payment is settled
    here and the period granted; no provider is involved. ``reference`` makes creation
    idempotent (renewals pass a key per period); a concurrent create with the same reference gets
    the existing row.
    """
    if reference:
        existing = BillingPayment.objects.filter(reference=reference).first()
        if existing is not None:
            return existing
    if purpose == BillingPayment.CHECKOUT and has_unconfirmed_charge(account):
        raise UnresolvedPayment()
    try:
        return _create_payment(
            account, plan, billing_period, provider, purpose, reference
        )
    except IntegrityError:
        if reference:
            existing = BillingPayment.objects.filter(reference=reference).first()
            if existing is not None:
                return existing
        raise


@transaction.atomic
def _create_payment(account, plan, billing_period, provider, purpose, reference):
    gross = price_for(plan, billing_period)
    unused = ZERO
    if purpose == BillingPayment.CHECKOUT:
        sub = (
            Subscription.objects.filter(account=account).select_related("plan").first()
        )
        unused = unused_value(sub, plan, billing_period)
    discount = min(unused, gross)
    payment = BillingPayment.objects.create(
        account=account,
        reference=reference or BillingPayment.new_reference(),
        provider=provider,
        purpose=purpose,
        plan=plan,
        billing_period=billing_period,
        currency=billing_currency(),
        gross_amount=gross,
        unused_time_value=unused,
        amount_due=gross - discount,
    )
    if payment.amount_due > 0:
        applied = credit.apply(payment, max_amount=payment.amount_due)
        if applied:
            payment.credit_applied = applied
            payment.amount_due -= applied
            payment.save(update_fields=["credit_applied", "amount_due", "updated_at"])
    if payment.amount_due <= 0:
        payment.provider = CREDIT
        payment.status = BillingPayment.SUCCEEDED
        payment.paid_at = timezone.now()
        payment.save(update_fields=["provider", "status", "paid_at", "updated_at"])
        _grant_unused_excess(payment)
        activate_subscription(account, plan, CREDIT, billing_period=billing_period)
    return payment


def _grant_unused_excess(payment) -> None:
    """Unused time worth more than the new price becomes credit (only once the new period is
    actually paid, so an abandoned checkout leaves the old plan untouched)."""
    excess = payment.unused_time_value - payment.gross_amount
    if excess > 0:
        credit.grant(
            payment.account,
            excess,
            reason=CreditGrant.PRORATION,
            note=f"Unused time left over after moving to {payment.plan.name}",
        )


def claim_for_charge(payment) -> bool:
    """Mark a pending payment as being charged. Exactly one caller gets True, so overlapping
    renewal runs can never charge the same saved card twice for one payment. From here on the
    charge counts as sent (``charge_started_at``), so it is looked up, never sent again."""
    now = timezone.now()
    return bool(
        BillingPayment.objects.filter(
            pk=payment.pk, status=BillingPayment.PENDING, provider_ref=""
        ).update(provider_ref=CHARGING, charge_started_at=now, updated_at=now)
    )


def mark_charge_started(payment) -> None:
    """A provider is about to move money for ``payment`` (e.g. capture an approved order). Until
    the outcome is known the payment is in flight: it isn't abandoned, the account can't pay for
    the same thing again, and abandon_stale reconciles it rather than giving it up."""
    if payment.charge_started_at is None:
        now = timezone.now()
        BillingPayment.objects.filter(
            pk=payment.pk, charge_started_at__isnull=True
        ).update(charge_started_at=now)
        payment.charge_started_at = now


def clear_charge_started(payment) -> None:
    """The provider definitely refused, so no money moved: the payment is an ordinary unfinished
    one again (abandoned after a day, or replaced by a new checkout)."""
    if payment.charge_started_at is not None:
        BillingPayment.objects.filter(pk=payment.pk).update(charge_started_at=None)
        payment.charge_started_at = None


def _in_flight():
    """Open payments whose charge may already have taken money: one was sent and hasn't
    settled, or its outcome couldn't be confirmed (UNRESOLVED)."""
    return Q(resolution=BillingPayment.UNRESOLVED) | Q(
        status=BillingPayment.PENDING, charge_started_at__isnull=False
    )


def abandon_open_checkouts(account) -> None:
    """Give back the credit held by checkouts the customer started but didn't finish, before they
    start another one. Bank transfers awaiting review, and payments whose charge may already have
    gone through (in flight), are kept."""
    for payment in BillingPayment.objects.filter(
        account=account,
        purpose=BillingPayment.CHECKOUT,
        status__in=[BillingPayment.PENDING, BillingPayment.REQUIRES_ACTION],
        manual_requests__isnull=True,
    ).exclude(_in_flight()):
        _abandon(payment)


def abandon_stale() -> int:
    """Payments still open after ABANDON_AFTER (beat task, hourly).

    A checkout nobody finished releases its credit. A renewal charge the provider never
    confirmed is treated as unpaid, so the subscription goes past due (and through grace to
    expiry) instead of staying active on a period that has ended. If the provider confirms it
    later, record_result still honours it.

    A payment in flight (a charge was sent: a renewal charge, or a PayPal capture) is first looked
    up with a provider that can ``reconcile``. Still pending at the provider (e.g. under review)
    keeps it waiting for the provider's webhook. No answer at all marks it UNRESOLVED rather than
    giving it up: giving it up would let the customer pay the same period again while the first
    charge may have gone through. UNRESOLVED payments are looked up again on every run, and so
    is any payment in flight for RECHECK_IN_FLIGHT_AFTER with a provider that can reconcile;
    one still pending after ALERT_PENDING_AFTER is escalated to an operator.
    """
    now = timezone.now()
    cutoff = now - ABANDON_AFTER
    open_statuses = [BillingPayment.PENDING, BillingPayment.REQUIRES_ACTION]
    recheck = Q(resolution=BillingPayment.UNRESOLVED) | Q(
        status=BillingPayment.PENDING,
        charge_started_at__lt=now - RECHECK_IN_FLIGHT_AFTER,
    )
    count = 0
    for payment in BillingPayment.objects.filter(
        Q(created_at__lt=cutoff) | recheck,
        purpose=BillingPayment.CHECKOUT,
        status__in=open_statuses,
        manual_requests__isnull=True,
    ):
        if payment.charge_started_at is not None and _reconcile(payment):
            continue
        if (
            payment.resolution == BillingPayment.UNRESOLVED
            or payment.created_at >= cutoff
        ):
            continue  # unresolved: only a confirmed outcome or an operator settles it
        _abandon(payment)
        count += 1
    for payment in BillingPayment.objects.filter(
        Q(updated_at__lt=cutoff) | recheck,
        purpose=BillingPayment.RENEWAL,
        status=BillingPayment.PENDING,
    ):
        if payment.charge_started_at is not None and _reconcile(payment):
            continue
        if (
            payment.resolution == BillingPayment.UNRESOLVED
            or payment.updated_at >= cutoff
        ):
            continue
        fail(payment, "The provider never confirmed the renewal charge")
        count += 1
    return count


def _reconcile(payment) -> bool:
    """Ask the provider what happened to a charge it was sent. True when the payment was dealt
    with here (settled, confirmed still pending, or marked UNRESOLVED) and must not be given up.
    False when the provider can't look charges up, so the old behaviour (give up after a day)
    applies."""
    from .providers import ProviderError, registry

    provider = registry.get(payment.provider)
    if provider is None or not provider.can_reconcile:
        return False
    try:
        result = provider.reconcile(payment)
    except ProviderError as exc:
        logger.warning(
            "reconcile: %s %s failed: %s", payment.provider, payment.reference, exc
        )
        result = None
    if result is None:
        mark_unresolved(payment)
        return True
    # PENDING here is a known state (the provider is still deciding, e.g. a review); its webhook
    # will settle it, and it stays in flight meanwhile, unless the provider said nothing was
    # charged (then it is an ordinary unfinished payment again).
    settled = record_result(provider.code, result) or payment
    if (
        result.status == PENDING
        and settled.status == BillingPayment.PENDING
        and settled.charge_started_at is not None
        and timezone.now() - settled.charge_started_at >= ALERT_PENDING_AFTER
    ):
        mark_unresolved(settled)
    return True


@transaction.atomic
def mark_unresolved(payment) -> None:
    """Nobody can yet say whether this charge went through. It won't be charged again and the
    account can't pay for the same thing again until it settles (the provider confirms, or an
    operator does). A renewal's subscription is left as it is rather than sent to dunning: the
    customer couldn't use the pay link anyway, and may well have paid. Idempotent."""
    payment = BillingPayment.objects.select_for_update().get(pk=payment.pk)
    if payment.status not in (BillingPayment.PENDING, BillingPayment.REQUIRES_ACTION):
        return
    if payment.resolution == BillingPayment.UNRESOLVED:
        return
    payment.resolution = BillingPayment.UNRESOLVED
    payment.save(update_fields=["resolution", "updated_at"])
    from apps.core import audit

    logger.error(
        "payment %s (%s): charge outcome unknown; needs an operator",
        payment.reference,
        payment.provider,
    )
    audit.system(
        "billing.unresolved",
        account=payment.account,
        target=payment.reference,
        provider=payment.provider,
        amount=str(payment.amount_due),
        currency=payment.currency,
    )


def has_unconfirmed_charge(account) -> bool:
    """Is a charge for this account in flight? Then another payment could charge it twice."""
    return (
        BillingPayment.objects.filter(
            account=account,
            status__in=[BillingPayment.PENDING, BillingPayment.REQUIRES_ACTION],
        )
        .filter(_in_flight())
        .exists()
    )


@transaction.atomic
def _abandon(payment) -> None:
    payment = BillingPayment.objects.select_for_update().get(pk=payment.pk)
    if payment.status not in (BillingPayment.PENDING, BillingPayment.REQUIRES_ACTION):
        return
    payment.status = BillingPayment.ABANDONED
    payment.save(update_fields=["status", "updated_at"])
    credit.restore(payment)


def record_result(provider_code: str, result: ProviderResult) -> BillingPayment | None:
    """Apply what ``provider_code`` reports about one of our payments. Idempotent."""
    if result.status == REFUNDED:
        return record_refund(provider_code, result)
    with transaction.atomic():
        payment = (
            BillingPayment.objects.select_for_update()
            .select_related("account", "plan")
            .filter(reference=result.reference, provider=provider_code)
            .first()
        )
        if payment is None:
            logger.warning(
                "record_result: no %s payment with reference=%s",
                provider_code,
                result.reference,
            )
            return None
        if payment.status == BillingPayment.SUCCEEDED:
            return payment  # already granted; a replay or the return racing the webhook

        if result.provider_ref:
            payment.provider_ref = result.provider_ref[:255]
        if result.raw:
            payment.raw = result.raw
        if (
            result.status in (SUCCEEDED, FAILED, REQUIRES_ACTION)
            and payment.resolution == BillingPayment.UNRESOLVED
        ):
            payment.resolution = ""
            from apps.core import audit

            audit.system(
                "billing.resolved",
                account=payment.account,
                target=payment.reference,
                status=result.status,
            )

        if result.status == SUCCEEDED:
            if not covers(
                result.amount, result.currency, payment.amount_due, payment.currency
            ):
                logger.error(
                    "record_result: amount mismatch payment=%s paid=%s %s due=%s %s",
                    payment.reference,
                    result.amount,
                    result.currency,
                    payment.amount_due,
                    payment.currency,
                )
                fail(payment, "Amount or currency did not match")
                return payment
            _succeed(payment, provider_code, result)
        elif result.status in (FAILED, REQUIRES_ACTION):
            if result.status == REQUIRES_ACTION:
                payment.action_url = (result.action_url or "")[:1000]
            fail(
                payment,
                result.failure_reason
                or (
                    "Needs customer action"
                    if result.status == REQUIRES_ACTION
                    else "Declined"
                ),
                status=(
                    BillingPayment.REQUIRES_ACTION
                    if result.status == REQUIRES_ACTION
                    else BillingPayment.FAILED
                ),
            )
        elif result.status == PENDING:
            payment.save()
    return payment


def _succeed(payment, provider_code, result) -> None:
    # fail() gave the credit back for each of these, so it is taken again below.
    was_given_up = payment.status in (
        BillingPayment.ABANDONED,
        BillingPayment.FAILED,
        BillingPayment.REQUIRES_ACTION,
    )
    payment.status = BillingPayment.SUCCEEDED
    payment.failure_reason = ""
    payment.paid_at = timezone.now()
    payment.save()
    if payment.refunds.exists():
        # A refund arrived before the payment was confirmed; it counts now.
        _refresh_refund_totals(payment)
    if was_given_up and payment.credit_applied:
        # Its credit went back when it was given up; the customer paid the reduced amount, so
        # take the credit again. If it has since been spent the payment is still honoured.
        reclaimed = credit.apply(payment, max_amount=payment.credit_applied)
        if reclaimed < payment.credit_applied:
            logger.warning(
                "record_result: payment=%s paid late; only %s of %s credit reclaimed",
                payment.reference,
                reclaimed,
                payment.credit_applied,
            )
    _grant_unused_excess(payment)
    if result.saved_method and result.saved_method.token:
        save_method(payment.account, provider_code, result.saved_method)
    activate_subscription(
        payment.account,
        payment.plan,
        provider_code,
        billing_period=payment.billing_period,
    )


def record_refund(provider_code: str, result: ProviderResult) -> BillingPayment | None:
    """Record one refund, reversal or chargeback of a payment. Idempotent on ``refund_id``.

    The payment keeps its status (a later replay of the original success must still find it
    granted) and access is left alone: the operator decides what a refund means for the
    subscription. ``refund_state`` is full only once the refunds add up to what was paid."""
    if not result.refund_id or result.refund_amount is None:
        logger.error(
            "record_refund: %s refund for %s has no id or amount",
            provider_code,
            result.reference,
        )
        return None
    with transaction.atomic():
        payment = (
            BillingPayment.objects.select_for_update()
            .select_related("account")
            .filter(reference=result.reference, provider=provider_code)
            .first()
        )
        if payment is None:
            logger.warning(
                "record_refund: no %s payment with reference=%s",
                provider_code,
                result.reference,
            )
            return None
        currency = (result.currency or payment.currency).upper()
        _, created = BillingRefund.objects.get_or_create(
            provider=provider_code,
            provider_refund_id=result.refund_id[:255],
            defaults={
                "payment": payment,
                "kind": result.refund_kind,
                "amount": result.refund_amount,
                "currency": currency,
                "raw": result.raw or {},
            },
        )
        if not created:
            return payment
        if payment.status != BillingPayment.SUCCEEDED or currency != payment.currency:
            # Kept as a record, but it only counts once the payment is paid (and only in the
            # payment's currency): _succeed recomputes the totals when it is.
            logger.error(
                "record_refund: %s %s %s for payment=%s (status=%s, %s)",
                result.refund_kind,
                result.refund_amount,
                currency,
                payment.reference,
                payment.status,
                payment.currency,
            )
        _refresh_refund_totals(payment)

        from apps.core import audit

        audit.system(
            f"billing.{result.refund_kind}",
            account=payment.account,
            target=payment.reference,
            provider=provider_code,
            refund_id=result.refund_id,
            amount=str(result.refund_amount),
            currency=currency,
            refunded_total=str(payment.refunded_amount),
            refund_state=payment.refund_state,
        )
        logger.warning(
            "payment %s: %s of %s %s (total refunded %s); operator to decide on access",
            payment.reference,
            result.refund_kind,
            result.refund_amount,
            currency,
            payment.refunded_amount,
        )
    return payment


def _refresh_refund_totals(payment) -> None:
    """refunded_amount / refund_state from the BillingRefund rows. Nothing counts until the
    payment is paid, and only refunds in the payment's currency are added up."""
    total = ZERO
    if payment.status == BillingPayment.SUCCEEDED:
        total = sum(
            (r.amount for r in payment.refunds.filter(currency=payment.currency)), ZERO
        )
    if total <= 0:
        state = BillingPayment.REFUND_NONE
    elif total >= payment.amount_due:
        state = BillingPayment.REFUND_FULL
    else:
        state = BillingPayment.REFUND_PARTIAL
    if (payment.refunded_amount, payment.refund_state) != (total, state):
        payment.refunded_amount = total
        payment.refund_state = state
        payment.save(update_fields=["refunded_amount", "refund_state", "updated_at"])


@transaction.atomic
def fail(payment, reason, status=BillingPayment.FAILED) -> None:
    """The payment won't complete: give its credit back; an unpaid renewal makes the
    subscription past due."""
    payment.status = status
    payment.failure_reason = (reason or "")[:255]
    payment.save()
    credit.restore(payment)
    if payment.purpose == BillingPayment.RENEWAL:
        mark_past_due(payment.account, payment=payment)


@transaction.atomic
def mark_past_due(account, *, payment=None) -> None:
    sub = Subscription.objects.select_for_update().filter(account=account).first()
    if sub is None or sub.status not in (Subscription.ACTIVE, Subscription.PAST_DUE):
        return
    first_time = sub.past_due_since is None
    sub.status = Subscription.PAST_DUE
    sub.past_due_since = sub.past_due_since or timezone.now()
    sub.save(update_fields=["status", "past_due_since", "updated_at"])
    if first_time:
        from .tasks import send_billing_notice

        transaction.on_commit(
            lambda: send_billing_notice.delay(
                account.pk, "unpaid", payment.pk if payment else None
            )
        )


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def save_method(account, provider_code, data) -> SavedPaymentMethod:
    """Remember a chargeable method as the account's default."""
    fingerprint = token_fingerprint(data.token)
    fields = {
        "customer_ref": (data.customer_ref or "")[:255],
        "brand": (data.brand or "")[:30],
        "last4": (data.last4 or "")[-4:],
        "expiry": (data.expiry or "")[:7],
        "label": (data.label or "")[:120],
        "provider_account": (data.provider_account or _merchant_key(provider_code))[
            :120
        ],
        "is_default": True,
    }
    method, created = SavedPaymentMethod.objects.get_or_create(
        account=account,
        provider=provider_code,
        token_fingerprint=fingerprint,
        defaults={"token": data.token, **fields},
    )
    if not created:
        for key, value in fields.items():
            setattr(method, key, value)
        method.save(update_fields=list(fields))
    SavedPaymentMethod.objects.filter(account=account, is_default=True).exclude(
        pk=method.pk
    ).update(is_default=False)
    return method


def _merchant_key(provider_code) -> str:
    from .providers import registry

    provider = registry.get(provider_code)
    return provider.account_key() if provider else ""


def default_method(account, provider_code) -> SavedPaymentMethod | None:
    """The method to charge, never one saved under a different merchant account than the
    provider's current one (when the provider tells them apart)."""
    methods = SavedPaymentMethod.objects.filter(account=account, provider=provider_code)
    key = _merchant_key(provider_code)
    if key:
        methods = methods.filter(provider_account=key)
    return methods.order_by("-is_default", "-created_at").first()

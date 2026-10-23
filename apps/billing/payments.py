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
from django.utils import timezone

from . import credit
from .models import BillingPayment, CreditGrant, SavedPaymentMethod, Subscription
from .money import billing_currency, covers
from .pricing import price_for
from .providers import FAILED, PENDING, REQUIRES_ACTION, SUCCEEDED, ProviderResult
from .services import activate_subscription, unused_value

logger = logging.getLogger(__name__)

CREDIT = "credit"
# provider_ref of a renewal whose saved-method charge is in flight (see claim_for_charge).
CHARGING = "charging"
ZERO = Decimal("0.00")
# A checkout nobody finished gives its credit back after this long.
ABANDON_AFTER = timedelta(hours=24)


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
    renewal runs can never charge the same saved card twice for one payment."""
    return bool(
        BillingPayment.objects.filter(
            pk=payment.pk, status=BillingPayment.PENDING, provider_ref=""
        ).update(provider_ref=CHARGING, updated_at=timezone.now())
    )


def abandon_open_checkouts(account) -> None:
    """Give back the credit held by checkouts the customer started but didn't finish, before they
    start another one. Bank transfers awaiting review are kept."""
    for payment in BillingPayment.objects.filter(
        account=account,
        purpose=BillingPayment.CHECKOUT,
        status__in=[BillingPayment.PENDING, BillingPayment.REQUIRES_ACTION],
        manual_requests__isnull=True,
    ):
        _abandon(payment)


def abandon_stale() -> int:
    """Payments still open after ABANDON_AFTER (beat task).

    A checkout nobody finished releases its credit. A renewal charge the provider never
    confirmed is treated as unpaid, so the subscription goes past due (and through grace to
    expiry) instead of staying active on a period that has ended. If the provider confirms it
    later, record_result still honours it.
    """
    cutoff = timezone.now() - ABANDON_AFTER
    open_statuses = [BillingPayment.PENDING, BillingPayment.REQUIRES_ACTION]
    count = 0
    for payment in BillingPayment.objects.filter(
        purpose=BillingPayment.CHECKOUT,
        status__in=open_statuses,
        created_at__lt=cutoff,
        manual_requests__isnull=True,
    ):
        _abandon(payment)
        count += 1
    for payment in BillingPayment.objects.filter(
        purpose=BillingPayment.RENEWAL,
        status=BillingPayment.PENDING,
        updated_at__lt=cutoff,
    ):
        fail(payment, "The provider never confirmed the renewal charge")
        count += 1
    return count


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
    was_given_up = payment.status in (BillingPayment.ABANDONED, BillingPayment.FAILED)
    payment.status = BillingPayment.SUCCEEDED
    payment.failure_reason = ""
    payment.paid_at = timezone.now()
    payment.save()
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


def default_method(account, provider_code) -> SavedPaymentMethod | None:
    return (
        SavedPaymentMethod.objects.filter(account=account, provider=provider_code)
        .order_by("-is_default", "-created_at")
        .first()
    )

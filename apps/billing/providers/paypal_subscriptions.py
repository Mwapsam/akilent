"""PayPal Subscriptions v1: PayPal keeps the renewal schedule and takes each payment; Akilent
grants every period, and only from a payment that completed. See docs/billing/paypal/CONTRACT.md
§8c and §8e.

- Each Akilent plan, term and price has a PayPal plan (``PayPalPlan``), made on first use. Its
  first cycle is a one-cycle TRIAL at the full price, so a subscription can override just the
  first payment (unused paid time and account credit off), then REGULAR cycles at the full price.
  Cycles are counted in days to match Akilent's periods (``pricing.PERIOD_DAYS``).
- The checkout payment's reference is the subscription's ``custom_id``; PayPal copies it onto
  every payment (a v1 sale's ``custom``) along with the subscription id
  (``billing_agreement_id``). The first payment completes the checkout payment; each later one
  becomes a renewal payment of its own (``payments.recurring_payment``), keyed by the sale id.
- The subscription's status is never read as paid: it is ACTIVE while its first payment is still
  under review. Only a sale fetched with our credentials, for this subscription, counts.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, timedelta

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.shortcuts import redirect
from django.utils import timezone

from ..models import BillingPayment, PayPalPlan, Subscription
from ..money import quantize, to_decimal
from ..paypal import PayPalError
from ..pricing import price_for
from .base import (
    FAILED,
    PENDING,
    REFUNDED,
    SUCCEEDED,
    ProviderError,
    ProviderResult,
    return_url,
)

logger = logging.getLogger(__name__)

# PayPal cycle length per Akilent term, in step with pricing.PERIOD_DAYS. PayPal counts at most
# 365 days, so two years is two calendar years (one day longer in a leap year; the renewal
# engine allows RECURRING_GRACE for that).
FREQUENCY = {
    Subscription.MONTHLY: ("DAY", 30),
    Subscription.QUARTERLY: ("DAY", 91),
    Subscription.ANNUALLY: ("DAY", 365),
    Subscription.BIENNIAL: ("YEAR", 2),
}
# v1 sale state -> our status. Anything not listed is PENDING, never paid.
SALE_STATE = {
    "completed": SUCCEEDED,
    "partially_refunded": SUCCEEDED,
    "pending": PENDING,
    "denied": FAILED,
    "refunded": FAILED,
    "reversed": FAILED,
}
ENDED = ("CANCELLED", "SUSPENDED", "EXPIRED")
NOT_APPROVED = ("APPROVAL_PENDING",)
# A checkout payment that starts a subscription (raw["kind"]), and a renewal PayPal charged.
CHECKOUT_KIND = "subscription"
RENEWAL_KIND = "subscription_sale"
SALE_EVENTS = (
    "PAYMENT.SALE.COMPLETED",
    "PAYMENT.SALE.PENDING",
    "PAYMENT.SALE.DENIED",
)
SALE_REFUND_EVENTS = ("PAYMENT.SALE.REFUNDED", "PAYMENT.SALE.REVERSED")
ENDED_EVENTS = (
    "BILLING.SUBSCRIPTION.CANCELLED",
    "BILLING.SUBSCRIPTION.SUSPENDED",
    "BILLING.SUBSCRIPTION.EXPIRED",
)
ACTIVATED_EVENT = "BILLING.SUBSCRIPTION.ACTIVATED"
# How far back the daily sync looks for payments.
SYNC_WINDOW = timedelta(days=40)


def enabled() -> bool:
    return bool(getattr(settings, "PAYPAL_SUBSCRIPTIONS", False))


def _stamp(when) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def _link(resource: dict, rel: str) -> str:
    for link in resource.get("links") or []:
        if link.get("rel") == rel:
            return link.get("href") or ""
    return ""


# --- plans ---------------------------------------------------------------------------------------


def paypal_plan(client, account_key: str, plan, period: str, currency: str):
    """The PayPal plan for ``plan`` billed ``period`` at today's price, made if missing. The
    request ids are derived from the terms, so two checkouts racing to make it get one plan."""
    price = price_for(plan, period)
    terms = {
        "account_key": account_key,
        "plan": plan,
        "billing_period": period,
        "currency": currency,
        "price": price,
    }
    row = PayPalPlan.objects.filter(**terms).first()
    if row is not None:
        return row
    product_id = (
        PayPalPlan.objects.filter(account_key=account_key)
        .values_list("product_id", flat=True)
        .first()
    )
    if not product_id:
        product = client.create_product(
            {"name": "Akilent subscription", "type": "SERVICE", "category": "SOFTWARE"},
            request_id=f"product-{_digest(account_key)}",
        )
        product_id = product["id"]
    unit, count = FREQUENCY[period]
    value = {"value": f"{price:.2f}", "currency_code": currency}
    label = dict(Subscription.BILLING_PERIOD_CHOICES).get(period, period)
    created = client.create_plan(
        {
            "product_id": product_id,
            "name": f"{plan.name} ({label})"[:127],
            "billing_cycles": [
                {
                    "frequency": {"interval_unit": unit, "interval_count": count},
                    "tenure_type": tenure,
                    "sequence": sequence,
                    "total_cycles": total,
                    "pricing_scheme": {"fixed_price": value},
                }
                for sequence, tenure, total in ((1, "TRIAL", 1), (2, "REGULAR", 0))
            ],
            "payment_preferences": {
                "auto_bill_outstanding": True,
                "payment_failure_threshold": 3,
            },
        },
        request_id=f"plan-{_digest(repr(sorted((k, str(v)) for k, v in terms.items())))}",
    )
    row, _ = PayPalPlan.objects.get_or_create(
        **terms,
        defaults={"product_id": product_id, "paypal_plan_id": created["id"]},
    )
    return row


# --- checkout ------------------------------------------------------------------------------------


def start(provider, request, payment):
    """Send the customer to PayPal to approve a subscription whose first payment is
    ``payment.amount_due`` (the price less unused time and credit) and every later one the full
    price."""
    client = provider._client()
    try:
        row = paypal_plan(
            client,
            provider.account_key(),
            payment.plan,
            payment.billing_period,
            payment.currency,
        )
        body = {
            "plan_id": row.paypal_plan_id,
            "custom_id": payment.reference,
            "application_context": {
                "shipping_preference": "NO_SHIPPING",
                "user_action": "SUBSCRIBE_NOW",
                "return_url": return_url(request, payment),
                "cancel_url": request.build_absolute_uri("/billing/plans/"),
            },
        }
        first = quantize(payment.amount_due)
        if first != row.price:
            body["plan"] = {
                "billing_cycles": [
                    {
                        "sequence": 1,
                        "total_cycles": 1,
                        "pricing_scheme": {
                            "fixed_price": {
                                "value": f"{first:.2f}",
                                "currency_code": payment.currency,
                            }
                        },
                    }
                ]
            }
        request_id = payment.provider_request_id or f"sub-{payment.reference}"
        created = client.create_subscription(body, request_id=request_id)
    except PayPalError as exc:
        logger.error(
            "paypal subscription checkout failed for payment=%s: %s debug_id=%s",
            payment.reference,
            exc.issue or exc,
            exc.debug_id,
        )
        messages.error(request, "We couldn't start the payment. Please try again.")
        return redirect("/billing/plans/")
    approve = _link(created, "approve")
    if not created.get("id") or not approve:
        logger.error(
            "paypal subscription: no id or approval link for %s", payment.reference
        )
        messages.error(request, "We couldn't start the payment. Please try again.")
        return redirect("/billing/plans/")
    payment.provider_operation_id = created["id"]
    payment.provider_request_id = request_id
    payment.raw = {
        **(payment.raw or {}),
        "kind": CHECKOUT_KIND,
        "paypal_plan": row.paypal_plan_id,
    }
    payment.save(
        update_fields=[
            "provider_operation_id",
            "provider_request_id",
            "raw",
            "updated_at",
        ]
    )
    return redirect(approve)


def is_checkout(payment) -> bool:
    return (payment.raw or {}).get("kind") == CHECKOUT_KIND


def is_renewal(payment) -> bool:
    return (payment.raw or {}).get("kind") == RENEWAL_KIND


def handle_return(provider, request, payment) -> ProviderResult | None:
    # PayPal appends ?subscription_id=I-...; it must be the one we created for this payment.
    given = request.GET.get("subscription_id") or ""
    if not payment.provider_operation_id or (
        given and given != payment.provider_operation_id
    ):
        logger.warning(
            "paypal return: subscription %s isn't the one for payment=%s",
            given,
            payment.reference,
        )
        return None
    return checkout_result(provider, payment)


def checkout_result(provider, payment) -> ProviderResult | None:
    """Where the subscription a checkout payment started stands: its first payment, if PayPal
    has taken one. Used on the return, on ACTIVATED, and by reconcile."""
    from apps.billing.payments import clear_charge_started, mark_charge_started

    data = _get_subscription(provider, payment.provider_operation_id)
    problems = []
    if data.get("id") != payment.provider_operation_id:
        problems.append("subscription id")
    if data.get("custom_id") != payment.reference:
        problems.append("custom_id")
    if data.get("plan_id") != (payment.raw or {}).get("paypal_plan"):
        problems.append("plan")
    if problems:
        logger.error(
            "paypal: rejected subscription=%s for payment=%s (%s mismatch)",
            data.get("id"),
            payment.reference,
            ", ".join(problems),
        )
        return None
    status = data.get("status") or ""
    if status in NOT_APPROVED:
        return None
    # Approved: PayPal takes the first payment by itself from here, so the payment is in flight
    # (kept, looked up, and the account can't start paying for the same thing again).
    mark_charge_started(payment)
    sale = _first_sale(provider, payment)
    if sale is not None:
        return sale_result(payment, sale, expected_custom=payment.reference)
    if status in ENDED:
        # Ended before any payment: nothing was charged.
        clear_charge_started(payment)
    return ProviderResult(
        reference=payment.reference,
        status=PENDING,
        raw={**(payment.raw or {}), "subscription_status": status},
    )


def _first_sale(provider, payment) -> dict | None:
    """The subscription's first payment, read from the sale itself."""
    client = provider._client()
    start_at = payment.created_at - timedelta(hours=1)
    try:
        transactions = client.subscription_transactions(
            payment.provider_operation_id,
            _stamp(start_at),
            _stamp(timezone.now() + timedelta(hours=1)),
        )
    except PayPalError as exc:
        if exc.status == 404:
            return None  # PayPal's answer for a subscription with no payments yet
        raise ProviderError(f"couldn't list payments: {exc.issue or exc}") from exc
    for tx in sorted(transactions, key=lambda t: t.get("time") or ""):
        sale = _get_sale(provider, tx.get("id"))
        if sale.get("billing_agreement_id") == payment.provider_operation_id:
            return sale
    return None


# --- payments ------------------------------------------------------------------------------------


def sale_result(payment, sale: dict, *, expected_custom: str) -> ProviderResult | None:
    """A result for ``payment`` from a sale, only if the sale belongs to its subscription."""
    problems = []
    if sale.get("billing_agreement_id") != payment.provider_operation_id:
        problems.append("subscription id")
    if sale.get("custom") != expected_custom:
        problems.append("custom")
    if problems:
        logger.error(
            "paypal: rejected sale=%s for payment=%s (%s mismatch)",
            sale.get("id"),
            payment.reference,
            ", ".join(problems),
        )
        return None
    state = (sale.get("state") or "").lower()
    mapped = SALE_STATE.get(state, PENDING)
    amount = sale.get("amount") or {}
    reason = sale.get("reason_code") or ""
    return ProviderResult(
        reference=payment.reference,
        status=mapped,
        provider_ref=sale.get("id", ""),
        amount=to_decimal(amount.get("total")),
        currency=(amount.get("currency") or "").upper(),
        failure_reason=f"PayPal: {reason or state}"[:255] if mapped == FAILED else "",
        raw={
            **(payment.raw or {}),
            "sale": sale.get("id"),
            "state": state,
            "reason": reason,
        },
        recurring_id=payment.provider_operation_id if mapped == SUCCEEDED else "",
    )


def record_sale(provider, sale_id: str) -> BillingPayment | None:
    """Apply one subscription payment, whichever way we heard of it (webhook or sync)."""
    from apps.billing.payments import mark_charge_started, record_result

    sale = _get_sale(provider, sale_id)
    first, payment = payment_for_sale(provider, sale)
    if payment is None:
        return None
    result = sale_result(payment, sale, expected_custom=first.reference)
    if result is None:
        return None
    # PayPal has charged (or is charging): in flight until settled, never abandoned.
    mark_charge_started(payment)
    return record_result(provider.code, result)


def payment_for_sale(provider, sale: dict):
    """(the subscription's checkout payment, the payment this sale pays) or (…, None).

    A sale already recorded keeps its payment. Otherwise it pays the checkout payment while
    that isn't paid yet (the first payment, or a retry of it); after that each sale is a
    renewal, but only of the subscription currently renewing the plan."""
    sale_id = sale.get("id") or ""
    agreement = sale.get("billing_agreement_id") or ""
    if not sale_id or not agreement:
        return None, None  # not a subscription payment
    first = BillingPayment.objects.filter(
        provider=provider.code,
        reference=sale.get("custom") or "",
        provider_operation_id=agreement,
    ).first()
    if first is None or not is_checkout(first):
        logger.warning("paypal: sale %s isn't one of our subscriptions", sale_id)
        return None, None
    existing = BillingPayment.objects.filter(
        provider=provider.code, provider_ref=sale_id
    ).first()
    if existing is not None:
        return first, existing
    if first.status != BillingPayment.SUCCEEDED and (
        first.provider_ref in ("", sale_id) or first.status != BillingPayment.PENDING
    ):
        return first, first
    sub = Subscription.objects.filter(account=first.account).first()
    if sub is None or sub.recurring_id != agreement:
        # Money taken by a subscription that no longer renews this account's plan (replaced or
        # ended here): never granted; the operator refunds it, and it is stopped again.
        _orphan(provider, first, sale)
        return first, None
    row = PayPalPlan.objects.filter(
        paypal_plan_id=(first.raw or {}).get("paypal_plan", "")
    ).first()
    if row is None:
        logger.error("paypal: no plan on record for subscription %s", agreement)
        return first, None
    from apps.billing.payments import recurring_payment

    return first, recurring_payment(
        first.account,
        row.plan,
        row.billing_period,
        provider=provider.code,
        reference=f"ppsale_{sale_id}"[:64],
        amount=row.price,
        currency=row.currency,
        operation_id=agreement,
        provider_ref=sale_id,
        raw={"kind": RENEWAL_KIND},
    )


def _orphan(provider, first, sale) -> None:
    from apps.billing.tasks import stop_recurring
    from apps.core import audit

    amount = sale.get("amount") or {}
    logger.error(
        "paypal: sale %s (%s %s) on subscription %s, which no longer renews account=%s; "
        "refund it",
        sale.get("id"),
        amount.get("total"),
        amount.get("currency"),
        sale.get("billing_agreement_id"),
        first.account_id,
    )
    audit.system(
        "billing.orphan_charge",
        account=first.account,
        target=sale.get("id", ""),
        provider=provider.code,
        subscription=sale.get("billing_agreement_id", ""),
        amount=str(amount.get("total")),
        currency=amount.get("currency", ""),
        state=sale.get("state", ""),
    )
    token = f"{provider.code}:{sale.get('billing_agreement_id')}"
    with transaction.atomic():
        sub = (
            Subscription.objects.select_for_update()
            .filter(account=first.account)
            .first()
        )
        if sub is None or token in sub.recurring_stop_id.split():
            return
        sub.recurring_stop_id = " ".join([*sub.recurring_stop_id.split(), token])
        sub.save(update_fields=["recurring_stop_id", "updated_at"])
        account_id = sub.account_id
        transaction.on_commit(lambda: stop_recurring.delay(account_id))


def refund_result(provider, refund_id: str, kind: str) -> ProviderResult | None:
    try:
        refund = provider._client().get_sale_refund(refund_id)
    except PayPalError as exc:
        if exc.status == 404:
            logger.error(
                "paypal: sale refund %s can't be read; record it by hand", refund_id
            )
            return None
        raise ProviderError(f"couldn't read refund: {exc.issue or exc}") from exc
    state = (refund.get("state") or "").lower()
    if state in ("failed", "cancelled"):
        return None
    if state != "completed":
        raise ProviderError(f"refund {refund_id} is {state}; retry later")
    sale_id = refund.get("sale_id") or ""
    payment = BillingPayment.objects.filter(
        provider=provider.code, provider_ref=sale_id
    ).first()
    if not sale_id or payment is None:
        logger.warning("paypal: refund %s isn't for one of our payments", refund_id)
        return None
    amount = refund.get("amount") or {}
    return ProviderResult(
        reference=payment.reference,
        status=REFUNDED,
        currency=(amount.get("currency") or "").upper(),
        refund_id=refund_id,
        refund_amount=to_decimal(amount.get("total")),
        refund_kind=kind,
        raw={"refund": refund_id, "state": state, "sale": sale_id},
    )


# --- subscription state --------------------------------------------------------------------------


def activated(provider, subscription_id: str) -> None:
    """ACTIVATED says the buyer approved, not that anything was paid: look the first payment up
    (in case its own webhook is late or lost)."""
    from apps.billing.payments import record_result

    payment = BillingPayment.objects.filter(
        provider=provider.code, provider_operation_id=subscription_id
    ).first()
    if (
        payment is None
        or not is_checkout(payment)
        or payment.status == BillingPayment.SUCCEEDED
    ):
        return
    result = checkout_result(provider, payment)
    if result is not None:
        record_result(provider.code, result)


def ended(provider, subscription_id: str) -> None:
    """CANCELLED, SUSPENDED or EXPIRED, confirmed by reading the subscription."""
    from apps.billing.recurring import provider_ended

    data = _get_subscription(provider, subscription_id)
    status = data.get("status") or ""
    if data.get("id") != subscription_id or status not in ENDED:
        return
    provider_ended(provider.code, subscription_id, by_customer=status == "CANCELLED")


def stop(provider, subscription_id: str) -> None:
    try:
        provider._client().cancel_subscription(
            subscription_id, "Cancelled or replaced in Akilent"
        )
    except PayPalError as exc:
        if exc.status in (404, 422):
            # Already cancelled or expired (SUBSCRIPTION_STATUS_INVALID): it charges nothing.
            data = _get_subscription(provider, subscription_id)
            if data.get("status") in ENDED or exc.status == 404:
                return
        raise ProviderError(f"couldn't cancel: {exc.issue or exc}") from exc


def sync(provider, sub) -> None:
    """Record payments and an ending the webhooks may have missed."""
    subscription_id = sub.recurring_id
    data = _get_subscription(provider, subscription_id)
    now = timezone.now()
    try:
        transactions = provider._client().subscription_transactions(
            subscription_id, _stamp(now - SYNC_WINDOW), _stamp(now + timedelta(hours=1))
        )
    except PayPalError as exc:
        raise ProviderError(f"couldn't list payments: {exc.issue or exc}") from exc
    settled = (BillingPayment.SUCCEEDED, BillingPayment.FAILED)
    for tx in sorted(transactions, key=lambda t: t.get("time") or ""):
        sale_id = tx.get("id") or ""
        if (
            not sale_id
            or BillingPayment.objects.filter(
                provider=provider.code, provider_ref=sale_id, status__in=settled
            ).exists()
        ):
            continue
        record_sale(provider, sale_id)
    if (data.get("status") or "") in ENDED:
        ended(provider, subscription_id)


def _get_subscription(provider, subscription_id: str) -> dict:
    try:
        return provider._client().get_subscription(subscription_id)
    except PayPalError as exc:
        raise ProviderError(f"couldn't read subscription: {exc.issue or exc}") from exc


def _get_sale(provider, sale_id) -> dict:
    if not sale_id:
        return {}
    try:
        return provider._client().get_sale(sale_id)
    except PayPalError as exc:
        raise ProviderError(f"couldn't read sale: {exc.issue or exc}") from exc

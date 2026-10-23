"""Flutterwave: hosted one-off checkout (tx_ref = our payment reference) and tokenized card
charges for renewals. Mobile money can't be charged again without the customer, so those
renewals go out as a pay link. Payment plans (Flutterwave-side recurring) are no longer created;
``cancel_legacy`` winds down the ones that exist.

Every result is re-verified with Flutterwave's verify endpoint; neither the redirect nor the
webhook body is trusted on its own.
"""

from __future__ import annotations

import hmac
import json
import logging

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect

from ..flutterwave import FlutterwaveError, get_fw_client
from ..money import to_decimal
from .base import (
    FAILED,
    PENDING,
    SUCCEEDED,
    InvalidSignature,
    PaymentProvider,
    ProviderError,
    ProviderEvent,
    ProviderResult,
    SavedMethodData,
    payment_label,
    return_url,
)

logger = logging.getLogger(__name__)

_STATUS = {"successful": SUCCEEDED, "failed": FAILED, "cancelled": FAILED}


def _result(verified: dict) -> ProviderResult:
    card = verified.get("card") or {}
    customer = verified.get("customer") or {}
    saved = None
    if card.get("token"):
        saved = SavedMethodData(
            token=card["token"],
            customer_ref=customer.get("email", ""),
            brand=card.get("type", ""),
            last4=card.get("last_4digits", ""),
            expiry=card.get("expiry", ""),
        )
    status = _STATUS.get((verified.get("status") or "").lower(), PENDING)
    return ProviderResult(
        reference=verified.get("tx_ref") or "",
        status=status,
        provider_ref=str(verified.get("id") or ""),
        amount=to_decimal(verified.get("amount")),
        currency=(verified.get("currency") or "").upper(),
        saved_method=saved,
        failure_reason=(verified.get("processor_response") or "")[:255]
        if status == FAILED
        else "",
        raw={
            "id": verified.get("id"),
            "status": verified.get("status"),
            "payment_type": verified.get("payment_type"),
        },
    )


class FlutterwaveProvider(PaymentProvider):
    code = "flutterwave"
    display_name = "Flutterwave"
    off_session_charge = True

    def is_configured(self) -> bool:
        return bool(
            getattr(settings, "FLUTTERWAVE_SECRET_KEY", None)
            and getattr(settings, "FLUTTERWAVE_WEBHOOK_HASH", None)
        )

    def config_status(self) -> str:
        missing = [
            name
            for name in ("FLUTTERWAVE_SECRET_KEY", "FLUTTERWAVE_WEBHOOK_HASH")
            if not getattr(settings, name, None)
        ]
        return f"Missing {', '.join(missing)}." if missing else ""

    def start_checkout(self, request, payment):
        account = payment.account
        try:
            link = get_fw_client().initialize_payment(
                tx_ref=payment.reference,
                amount=payment.amount_due,
                currency=payment.currency,
                customer_email=request.user.email or account.billing_email or "",
                customer_name=account.company_name or request.user.get_username(),
                redirect_url=return_url(request, payment),
                meta={"reference": payment.reference, "account_id": account.pk},
                payment_options=getattr(
                    settings, "FLUTTERWAVE_PAYMENT_OPTIONS", "card"
                ),
                title=payment_label(payment),
            )
        except FlutterwaveError as exc:
            logger.error(
                "flutterwave checkout failed for payment=%s: %s", payment.reference, exc
            )
            messages.error(request, "We couldn't start the payment. Please try again.")
            return redirect("/billing/plans/")
        return redirect(link)

    def reference_from_return(self, request) -> str | None:
        return request.GET.get("tx_ref") or request.GET.get("ref") or None

    def handle_return(self, request, payment) -> ProviderResult | None:
        transaction_id = request.GET.get("transaction_id")
        if not transaction_id:
            if request.GET.get("status") == "cancelled":
                return ProviderResult(
                    reference=payment.reference,
                    status=FAILED,
                    failure_reason="Cancelled",
                )
            return None
        try:
            verified = get_fw_client().verify_transaction(transaction_id)
        except FlutterwaveError as exc:
            logger.warning(
                "flutterwave return: verify failed tx=%s: %s", transaction_id, exc
            )
            return None
        result = _result(verified)
        # The transaction must be this payment's, not any successful one the customer has.
        if result.reference != payment.reference:
            logger.warning(
                "flutterwave return: tx=%s is for %s, not %s",
                transaction_id,
                result.reference,
                payment.reference,
            )
            return None
        return result

    def parse_webhook(self, request) -> list[ProviderEvent]:
        expected = getattr(settings, "FLUTTERWAVE_WEBHOOK_HASH", None) or ""
        received = request.headers.get("verif-hash") or ""
        if not expected or not hmac.compare_digest(
            received.encode(), expected.encode()
        ):
            raise InvalidSignature("invalid verif-hash")
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError as exc:
            raise InvalidSignature("body is not JSON") from exc

        event = payload.get("event") or payload.get("event.type") or ""
        data = payload.get("data") or {}
        key = f"flutterwave:{event}:{data.get('id')}"
        if event != "charge.completed" or not data.get("id"):
            return [ProviderEvent(key=key)]

        if (data.get("meta") or {}).get("order_id"):
            return [ProviderEvent(key=key, handler=lambda: _commerce_charge(data))]

        # Verify before acting; a failure raises, the view answers 500 and Flutterwave retries.
        try:
            verified = get_fw_client().verify_transaction(data["id"])
        except FlutterwaveError as exc:
            raise ProviderError(f"verify failed: {exc}") from exc
        result = _result(verified)

        from apps.billing.models import BillingPayment

        if BillingPayment.objects.filter(
            reference=result.reference, provider=self.code
        ).exists():
            return [ProviderEvent(key=key, result=result)]
        # Not one of our payments: a renewal of a legacy Flutterwave payment plan.
        return [ProviderEvent(key=key, handler=lambda: _legacy_charge(verified))]

    def charge_saved_method(self, method, payment) -> ProviderResult:
        email = method.customer_ref or payment.account.billing_email or ""
        try:
            data = get_fw_client().charge_token(
                token=method.token,
                email=email,
                amount=payment.amount_due,
                currency=payment.currency,
                tx_ref=payment.reference,
            )
        except FlutterwaveError as exc:
            return ProviderResult(
                reference=payment.reference,
                status=FAILED,
                failure_reason=str(exc)[:255],
            )
        if data.get("id"):
            try:
                data = get_fw_client().verify_transaction(data["id"])
            except FlutterwaveError as exc:
                logger.warning("flutterwave token charge verify failed: %s", exc)
                return ProviderResult(reference=payment.reference, status=PENDING)
        result = _result(data)
        result.reference = payment.reference
        return result

    def cancel_legacy(self, subscription) -> SavedMethodData | None:
        if subscription.fw_subscription_id:
            try:
                get_fw_client().cancel_subscription(subscription.fw_subscription_id)
            except FlutterwaveError as exc:
                raise ProviderError(str(exc)) from exc
        return None


# --- events that aren't our subscription payments -----------------------------------------------


def _commerce_charge(data: dict) -> None:
    """A Flutterwave charge for a commerce order (apps.commerce shares this webhook)."""
    from apps.billing.money import covers
    from apps.commerce.models import Payment
    from apps.commerce.services import mark_failed, mark_paid

    meta = data.get("meta") or {}
    payment = Payment.objects.filter(
        public_id=meta.get("payment_id"), order__public_id=meta.get("order_id")
    ).first()
    if payment is None:
        logger.warning("flutterwave commerce: no payment for meta=%s", meta)
        return
    try:
        verified = get_fw_client().verify_transaction(data.get("id"))
    except FlutterwaveError as exc:
        raise ProviderError(f"verify failed: {exc}") from exc
    if verified.get("status") != "successful" or not covers(
        verified.get("amount"),
        verified.get("currency") or "",
        payment.amount,
        payment.currency,
    ):
        logger.error(
            "flutterwave commerce: rejected payment=%s status=%s amount=%s %s",
            payment.public_id,
            verified.get("status"),
            verified.get("amount"),
            verified.get("currency"),
        )
        mark_failed(
            payment, error=f"verification rejected: status={verified.get('status')}"
        )
        return
    mark_paid(payment, transaction_id=str(data.get("id")), raw_payload=verified)


def _legacy_charge(verified: dict) -> None:
    """A renewal charge from a Flutterwave payment plan created before Akilent ran renewals.
    Matched to the subscription by customer email; granted only if it pays the full period."""
    from apps.billing.models import Subscription
    from apps.billing.money import billing_currency, covers
    from apps.billing.pricing import price_for
    from apps.billing.services import activate_subscription

    if verified.get("status") != "successful":
        return
    email = (verified.get("customer") or {}).get("email")
    sub = (
        Subscription.objects.select_related("plan", "account")
        .filter(fw_customer_email__iexact=email, fw_subscription_id__isnull=False)
        .exclude(fw_subscription_id="")
        .first()
        if email
        else None
    )
    if sub is None:
        logger.warning(
            "flutterwave legacy: no subscription for tx=%s", verified.get("id")
        )
        return
    if not covers(
        verified.get("amount"),
        verified.get("currency") or "",
        price_for(sub.plan, sub.billing_period),
        billing_currency(),
    ):
        logger.error(
            "flutterwave legacy: tx=%s doesn't cover the plan", verified.get("id")
        )
        return
    activate_subscription(
        sub.account, sub.plan, "flutterwave", billing_period=sub.billing_period
    )

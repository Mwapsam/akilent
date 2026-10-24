"""Stripe: hosted Checkout in payment mode that saves the card, and off-session PaymentIntents for
renewals. Akilent runs the subscription; Stripe never holds a recurring subscription for new
customers. Subscriptions created before that (mode=subscription) are wound down by
``cancel_legacy`` and their remaining invoice events are handled here until they run out."""

from __future__ import annotations

import logging

import stripe as stripe_sdk
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect
from django.utils import timezone

from ..money import from_minor_units, to_minor_units
from .base import (
    FAILED,
    PENDING,
    REQUIRES_ACTION,
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


def _dict(obj) -> dict:
    """Stripe SDK objects (v15+) are not dicts; convert at the boundary."""
    if obj is None:
        return {}
    return obj.to_dict() if hasattr(obj, "to_dict") else obj


def invoice_subscription_id(invoice: dict) -> str | None:
    """The subscription an invoice bills. API 2025-03+ (incl. dahlia) moved it under
    ``parent.subscription_details``; older API versions had a top-level ``subscription``."""
    parent = invoice.get("parent") or {}
    details = parent.get("subscription_details") or {}
    sub = details.get("subscription") or invoice.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    return sub or None


def _card(pm: dict, customer: str = "") -> SavedMethodData | None:
    if not pm or not pm.get("id"):
        return None
    card = pm.get("card") or {}
    expiry = ""
    if card.get("exp_month") and card.get("exp_year"):
        expiry = f"{int(card['exp_month']):02d}/{card['exp_year']}"
    return SavedMethodData(
        token=pm["id"],
        customer_ref=customer or pm.get("customer") or "",
        brand=card.get("brand", ""),
        last4=card.get("last4", ""),
        expiry=expiry,
    )


class StripeProvider(PaymentProvider):
    code = "stripe"
    display_name = "Stripe"
    checkout_origins = ("https://checkout.stripe.com",)
    off_session_charge = True

    def _api(self):
        stripe_sdk.api_key = settings.STRIPE_SECRET_KEY
        return stripe_sdk

    def is_configured(self) -> bool:
        return bool(
            getattr(settings, "STRIPE_SECRET_KEY", None)
            and getattr(settings, "STRIPE_WEBHOOK_SECRET", None)
        )

    def config_status(self) -> str:
        missing = [
            name
            for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET")
            if not getattr(settings, name, None)
        ]
        return f"Missing {', '.join(missing)}." if missing else ""

    # --- checkout --------------------------------------------------------------------------

    def start_checkout(self, request, payment):
        from apps.billing.payments import default_method

        api = self._api()
        currency = payment.currency.lower()
        saved = default_method(payment.account, self.code)
        params = {
            "mode": "payment",
            "line_items": [
                {
                    "price_data": {
                        "currency": currency,
                        "product_data": {"name": payment_label(payment)},
                        "unit_amount": to_minor_units(
                            payment.amount_due, payment.currency
                        ),
                    },
                    "quantity": 1,
                }
            ],
            "payment_intent_data": {
                "setup_future_usage": "off_session",
                "metadata": {"reference": payment.reference},
            },
            "client_reference_id": payment.reference,
            "metadata": {
                "reference": payment.reference,
                "account_id": str(payment.account_id),
            },
            "success_url": return_url(request, payment)
            + "&session_id={CHECKOUT_SESSION_ID}",
            "cancel_url": request.build_absolute_uri("/billing/plans/"),
        }
        if saved and saved.customer_ref:
            params["customer"] = saved.customer_ref
        else:
            params["customer_creation"] = "always"
            if request.user.email:
                params["customer_email"] = request.user.email
        try:
            session = api.checkout.Session.create(**params)
        except stripe_sdk.error.StripeError as exc:
            logger.error(
                "stripe checkout failed for payment=%s: %s", payment.reference, exc
            )
            messages.error(request, "We couldn't start the payment. Please try again.")
            return redirect("/billing/plans/")
        payment.provider_ref = session.id or ""
        payment.save(update_fields=["provider_ref", "updated_at"])
        return redirect(session.url or "/billing/plans/")

    def handle_return(self, request, payment) -> ProviderResult | None:
        session_id = request.GET.get("session_id") or payment.provider_ref
        if not session_id:
            return None
        try:
            session = _dict(
                self._api().checkout.Session.retrieve(
                    session_id, expand=["payment_intent.payment_method"]
                )
            )
        except stripe_sdk.error.StripeError as exc:
            logger.warning(
                "stripe return: could not retrieve session=%s: %s", session_id, exc
            )
            return None
        return self._session_result(session)

    def _session_result(self, session: dict) -> ProviderResult | None:
        if session.get("mode") != "payment":
            return None
        reference = session.get("client_reference_id") or (
            session.get("metadata") or {}
        ).get("reference")
        if not reference:
            return None
        paid = session.get("payment_status") in ("paid", "no_payment_required")
        currency = (session.get("currency") or "").upper()
        intent = session.get("payment_intent")
        if isinstance(intent, str) and paid:
            try:
                intent = _dict(
                    self._api().PaymentIntent.retrieve(
                        intent, expand=["payment_method"]
                    )
                )
            except stripe_sdk.error.StripeError:
                logger.warning("stripe: couldn't load payment_intent=%s", intent)
                intent = {"id": intent}
        intent = intent if isinstance(intent, dict) else {}
        pm = intent.get("payment_method")
        return ProviderResult(
            reference=reference,
            status=SUCCEEDED if paid else PENDING,
            provider_ref=intent.get("id") or session.get("id", ""),
            amount=from_minor_units(
                session.get("amount_total") or 0, currency or "USD"
            ),
            currency=currency,
            saved_method=_card(pm, session.get("customer") or "")
            if isinstance(pm, dict)
            else None,
            raw={"session": session.get("id"), "payment_intent": intent.get("id")},
        )

    def _intent_result(self, intent: dict) -> ProviderResult | None:
        reference = (intent.get("metadata") or {}).get("reference")
        if not reference:
            return None
        status = intent.get("status") or ""
        currency = (intent.get("currency") or "").upper()
        error = intent.get("last_payment_error") or {}
        mapped = {
            "succeeded": SUCCEEDED,
            "requires_action": REQUIRES_ACTION,
            "requires_payment_method": FAILED,
            "canceled": FAILED,
        }.get(status, PENDING)
        pm = intent.get("payment_method")
        return ProviderResult(
            reference=reference,
            status=mapped,
            provider_ref=intent.get("id", ""),
            amount=from_minor_units(
                intent.get("amount_received") or intent.get("amount") or 0,
                currency or "USD",
            ),
            currency=currency,
            saved_method=_card(pm, intent.get("customer") or "")
            if isinstance(pm, dict)
            else None,
            failure_reason=error.get("message", "")[:255],
            raw={"payment_intent": intent.get("id"), "status": status},
        )

    # --- webhooks --------------------------------------------------------------------------

    def parse_webhook(self, request) -> list[ProviderEvent]:
        secret = getattr(settings, "STRIPE_WEBHOOK_SECRET", None)
        if not secret:
            raise InvalidSignature("STRIPE_WEBHOOK_SECRET is not configured")
        self._api()
        try:
            event = stripe_sdk.Webhook.construct_event(
                request.body, request.headers.get("Stripe-Signature"), secret
            )
        except (ValueError, stripe_sdk.error.SignatureVerificationError) as exc:
            raise InvalidSignature(str(exc)) from exc

        kind = event["type"]
        obj = _dict(event["data"]["object"])
        key = f"stripe:{kind}:{event['id']}"

        if kind in (
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
        ):
            return [ProviderEvent(key=key, result=self._session_result(obj))]
        if kind == "checkout.session.async_payment_failed":
            result = self._session_result(obj)
            if result:
                result.status = FAILED
                result.failure_reason = "The payment didn't go through."
            return [ProviderEvent(key=key, result=result)]
        if kind in (
            "payment_intent.succeeded",
            "payment_intent.payment_failed",
            "payment_intent.requires_action",
        ):
            return [ProviderEvent(key=key, result=self._intent_result(obj))]

        # Legacy provider-side subscriptions (created before Akilent ran renewals itself).
        if kind == "invoice.payment_succeeded":
            return [ProviderEvent(key=key, handler=lambda: _legacy_invoice_paid(obj))]
        if kind == "invoice.payment_failed":
            return [ProviderEvent(key=key, handler=lambda: _legacy_invoice_failed(obj))]
        if kind == "customer.subscription.deleted":
            return [
                ProviderEvent(
                    key=key, handler=lambda: _legacy_subscription_deleted(obj)
                )
            ]
        return [ProviderEvent(key=key)]

    # --- renewals --------------------------------------------------------------------------

    def charge_saved_method(self, method, payment) -> ProviderResult:
        api = self._api()
        try:
            intent = api.PaymentIntent.create(
                amount=to_minor_units(payment.amount_due, payment.currency),
                currency=payment.currency.lower(),
                customer=method.customer_ref or None,
                payment_method=method.token,
                off_session=True,
                confirm=True,
                metadata={"reference": payment.reference},
                idempotency_key=f"charge-{payment.reference}",
            )
            return self._intent_result(_dict(intent)) or ProviderResult(
                reference=payment.reference, status=PENDING
            )
        except stripe_sdk.error.CardError as exc:
            intent = _dict(getattr(exc, "payment_intent", None) or {})
            if not intent and getattr(exc, "error", None) is not None:
                intent = _dict(getattr(exc.error, "payment_intent", None) or {})
            result = self._intent_result(intent) if intent else None
            if result is None:
                result = ProviderResult(reference=payment.reference, status=FAILED)
            if result.status not in (REQUIRES_ACTION, FAILED):
                result.status = FAILED
            result.failure_reason = (getattr(exc, "user_message", None) or str(exc))[
                :255
            ]
            return result
        except stripe_sdk.error.StripeError as exc:
            raise ProviderError(str(exc)) from exc

    def cancel_legacy(self, subscription) -> SavedMethodData | None:
        if not subscription.stripe_subscription_id:
            return None
        api = self._api()
        try:
            remote = _dict(
                api.Subscription.modify(
                    subscription.stripe_subscription_id,
                    cancel_at_period_end=True,
                    expand=["default_payment_method"],
                )
            )
        except stripe_sdk.error.StripeError as exc:
            raise ProviderError(str(exc)) from exc
        pm = remote.get("default_payment_method")
        customer = remote.get("customer") or subscription.stripe_customer_id or ""
        if isinstance(pm, dict):
            return _card(pm, customer)
        if customer:
            try:
                cust = _dict(
                    api.Customer.retrieve(
                        customer, expand=["invoice_settings.default_payment_method"]
                    )
                )
                pm = (cust.get("invoice_settings") or {}).get("default_payment_method")
                if isinstance(pm, dict):
                    return _card(pm, customer)
            except stripe_sdk.error.StripeError:
                pass
        return None


# --- legacy subscription events ----------------------------------------------------------------


def _legacy_sub(invoice_or_sub_id):
    from apps.billing.models import Subscription

    if not invoice_or_sub_id:
        return None
    return (
        Subscription.objects.select_related("plan", "account")
        .filter(stripe_subscription_id=invoice_or_sub_id)
        .first()
    )


def _legacy_invoice_paid(invoice: dict) -> None:
    from apps.billing.services import activate_subscription

    if invoice.get("billing_reason") != "subscription_cycle":
        return  # the first invoice was granted by checkout.session.completed at the time
    sub = _legacy_sub(invoice_subscription_id(invoice))
    if sub is None:
        return
    activate_subscription(
        sub.account, sub.plan, "stripe", billing_period=sub.billing_period
    )
    logger.info("stripe legacy: renewed account=%s", sub.account_id)


def _legacy_invoice_failed(invoice: dict) -> None:
    from apps.billing.payments import mark_past_due

    sub = _legacy_sub(invoice_subscription_id(invoice))
    if sub is not None:
        mark_past_due(sub.account)


def _legacy_subscription_deleted(remote: dict) -> None:
    """Stripe stopped a legacy subscription (we wound it down, or its dunning ran out). Akilent's
    renewal engine takes over at period end, so the access already paid for is kept."""
    from apps.billing.models import Subscription

    Subscription.objects.filter(stripe_subscription_id=remote.get("id")).update(
        stripe_subscription_id=None, updated_at=timezone.now()
    )

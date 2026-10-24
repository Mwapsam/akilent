"""PayPal: Orders v2 hosted checkout. Renewals go out as an emailed pay link
(``off_session_charge`` is False): saving PayPal wallets for later charges (Vault) is only
available to US merchant accounts, so it isn't used. See docs/billing/paypal/CONTRACT.md.

Our payment reference is the order's ``custom_id`` and ``invoice_id``. PayPal refuses a second
capture with the same invoice id (DUPLICATE_INVOICE_ID), so one payment can't be paid twice.

Nothing PayPal sends is trusted as-is. A return or webhook only says where to look; the order,
capture or refund is fetched with our credentials, and a result is produced only when it is
this payment's (order id, custom_id, invoice_id), paid to our merchant account. The order's own
status is never read as paid: an order can be COMPLETED while its capture is PENDING review.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect

from ..money import billing_currency, quantize, to_decimal
from ..paypal import PayPalError, PayPalUncertain, config, get_paypal_client
from .base import (
    FAILED,
    PENDING,
    REFUND,
    REFUNDED,
    REQUIRES_ACTION,
    REVERSAL,
    SUCCEEDED,
    InvalidSignature,
    PaymentProvider,
    ProviderError,
    ProviderEvent,
    ProviderResult,
    payment_label,
    return_url,
)

logger = logging.getLogger(__name__)

# Capture resource status -> our status. Anything not listed is PENDING, never paid.
CAPTURE_STATUS = {
    "COMPLETED": SUCCEEDED,
    "PENDING": PENDING,
    "DECLINED": FAILED,
    "FAILED": FAILED,
    # Partly refunded: the customer paid and kept most of it; each refund is recorded from its
    # own refund resource and the operator decides.
    "PARTIALLY_REFUNDED": SUCCEEDED,
    # Fully refunded: the money went back, so it never pays for a period (a payment that was
    # already granted keeps its grant; record_result ignores later results for it).
    "REFUNDED": FAILED,
}

CAPTURE_EVENTS = (
    "PAYMENT.CAPTURE.COMPLETED",
    "PAYMENT.CAPTURE.PENDING",
    "PAYMENT.CAPTURE.DECLINED",
    "PAYMENT.CAPTURE.DENIED",
)
# Capture refusals PayPal says to fix by sending the buyer back to approve again.
BUYER_CAN_RETRY = ("INSTRUMENT_DECLINED", "PAYER_ACTION_REQUIRED")
# Capture errors that mean no money moved. Any other error (PREVIOUS_REQUEST_IN_PROGRESS, 401,
# 403, 404, 429, an issue we don't know) is not taken as a refusal: the order is read back.
DEFINITE_REFUSALS = (
    "ORDER_NOT_APPROVED",
    "TRANSACTION_REFUSED",
    "PAYER_CANNOT_PAY",
    "ORDER_EXPIRED",
    "MAX_NUMBER_OF_PAYMENT_ATTEMPTS_EXCEEDED",
    *BUYER_CAN_RETRY,
)
# Order states in which no money has been taken.
UNCAPTURED_ORDER_STATUSES = (
    "CREATED",
    "SAVED",
    "APPROVED",
    "PAYER_ACTION_REQUIRED",
    "VOIDED",
)
REFUND_EVENTS = {
    "PAYMENT.CAPTURE.REFUNDED": REFUND,
    "PAYMENT.CAPTURE.REVERSED": REVERSAL,
}

TRANSMISSION_HEADERS = {
    "auth_algo": "PAYPAL-AUTH-ALGO",
    "cert_url": "PAYPAL-CERT-URL",
    "transmission_id": "PAYPAL-TRANSMISSION-ID",
    "transmission_sig": "PAYPAL-TRANSMISSION-SIG",
    "transmission_time": "PAYPAL-TRANSMISSION-TIME",
}

REQUIRED_SETTINGS = (
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "PAYPAL_WEBHOOK_ID",
    "PAYPAL_MERCHANT_ID",
)


def _first_capture(order: dict) -> dict:
    for unit in order.get("purchase_units") or []:
        captures = (unit.get("payments") or {}).get("captures") or []
        if captures:
            return captures[0]
    return {}


def _payee(order: dict) -> str:
    units = order.get("purchase_units") or [{}]
    return ((units[0].get("payee") or {}).get("merchant_id")) or ""


def _link(resource: dict, rel: str) -> str:
    for link in resource.get("links") or []:
        if link.get("rel") == rel:
            return link.get("href") or ""
    return ""


class PayPalProvider(PaymentProvider):
    code = "paypal"
    display_name = "PayPal"
    checkout_label = "Pay with PayPal"
    checkout_origins = ("https://www.paypal.com", "https://www.sandbox.paypal.com")
    resume_on_return = True
    off_session_charge = False
    can_reconcile = True

    @property
    def supported_currencies(self) -> frozenset:
        return frozenset(getattr(settings, "PAYPAL_CURRENCIES", None) or {"USD"})

    @staticmethod
    def _env_problem() -> str:
        env = config("PAYPAL_ENV")
        if env not in ("sandbox", "live"):
            return "PAYPAL_ENV must be set to sandbox or live."
        if (
            env == "sandbox"
            and not settings.DEBUG
            and not getattr(settings, "PAYPAL_ALLOW_SANDBOX", False)
        ):
            # Customers would pay with test money and still get a plan.
            return "PAYPAL_ENV is sandbox on a production server (set PAYPAL_ALLOW_SANDBOX)."
        return ""

    def is_configured(self) -> bool:
        return (
            all(config(name) for name in REQUIRED_SETTINGS) and not self._env_problem()
        )

    def config_status(self) -> str:
        problems = []
        missing = [name for name in REQUIRED_SETTINGS if not config(name)]
        if missing:
            problems.append(f"Missing {', '.join(missing)}.")
        if self._env_problem():
            problems.append(self._env_problem())
        if not self.supports_currency(billing_currency()):
            problems.append(
                f"PayPal is set up for {', '.join(sorted(self.supported_currencies))}, "
                f"but plans are priced in {billing_currency()}."
            )
        return " ".join(problems)

    def account_key(self) -> str:
        return f"{config('PAYPAL_ENV')}:{config('PAYPAL_MERCHANT_ID')}"

    # --- checkout --------------------------------------------------------------------------

    def start_checkout(self, request, payment):
        if not self.supports_currency(payment.currency):
            logger.error(
                "paypal: can't charge %s (payment=%s)",
                payment.currency,
                payment.reference,
            )
            messages.error(request, "PayPal can't take payments in this currency.")
            return redirect("/billing/plans/")
        request_id = payment.provider_request_id or f"order-{payment.reference}"
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "custom_id": payment.reference,
                    "invoice_id": payment.reference,
                    "description": payment_label(payment)[:127],
                    "amount": {
                        "currency_code": payment.currency,
                        "value": f"{quantize(payment.amount_due):.2f}",
                    },
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "return_url": return_url(request, payment),
                        "cancel_url": request.build_absolute_uri("/billing/plans/"),
                        "user_action": "PAY_NOW",
                        "shipping_preference": "NO_SHIPPING",
                    }
                }
            },
        }
        try:
            # Same request id for the same payment: starting again returns the same order.
            order = get_paypal_client().create_order(body, request_id=request_id)
        except PayPalError as exc:
            logger.error(
                "paypal checkout failed for payment=%s: %s debug_id=%s",
                payment.reference,
                exc.issue or exc,
                exc.debug_id,
            )
            messages.error(request, "We couldn't start the payment. Please try again.")
            return redirect("/billing/plans/")
        approve = _link(order, "payer-action") or _link(order, "approve")
        if not order.get("id") or not approve:
            logger.error(
                "paypal checkout: no order id or approval link for %s",
                payment.reference,
            )
            messages.error(request, "We couldn't start the payment. Please try again.")
            return redirect("/billing/plans/")
        payment.provider_operation_id = order["id"]
        payment.provider_request_id = request_id
        payment.save(
            update_fields=["provider_operation_id", "provider_request_id", "updated_at"]
        )
        return redirect(approve)

    def handle_return(self, request, payment) -> ProviderResult | None:
        # PayPal appends ?token=<order id>; it must be the order we created for this payment.
        token = request.GET.get("token") or ""
        if not payment.provider_operation_id or (
            token and token != payment.provider_operation_id
        ):
            logger.warning(
                "paypal return: order %s isn't the one for payment=%s",
                token,
                payment.reference,
            )
            return None
        return self._capture(payment)

    def _capture(self, payment) -> ProviderResult | None:
        """Capture this payment's order (or read it if already captured) and verify it.

        Only an open payment is captured. One that was abandoned (the customer started another
        checkout) or failed is left uncaptured even if its order gets approved later, so the
        customer can't pay twice; PayPal lets the approved order expire.

        Each attempt has its own PayPal-Request-Id: after a decline the buyer approves again
        (often with another funding source) and the next capture must not get PayPal's stored
        answer to the first one."""
        from apps.billing.models import BillingPayment
        from apps.billing.payments import (
            clear_charge_started,
            mark_charge_started,
            mark_unresolved,
        )

        if payment.status not in (
            BillingPayment.PENDING,
            BillingPayment.REQUIRES_ACTION,
        ):
            logger.info(
                "paypal: not capturing order %s of %s payment=%s",
                payment.provider_operation_id,
                payment.status,
                payment.reference,
            )
            return None
        client = self._client()
        order_id = payment.provider_operation_id
        attempt = int((payment.raw or {}).get("capture_attempt") or 0)
        request_id = f"capture-{payment.reference}" + (f"-{attempt}" if attempt else "")
        # Money may move from here: until we know, the payment is in flight (kept, reconciled).
        mark_charge_started(payment)
        try:
            order = client.capture_order(order_id, request_id=request_id)
        except PayPalUncertain:
            order = self._get_order(order_id)
        except PayPalError as exc:
            if exc.issue == "DUPLICATE_INVOICE_ID":
                # Another order with this reference was already captured: the money may be
                # there. Never a decline, and not something reconcile can settle from this
                # order; held for an operator to match up.
                logger.error(
                    "paypal: payment=%s already captured under another order (debug_id=%s)",
                    payment.reference,
                    exc.debug_id,
                )
                self._remember(payment, hold="duplicate_invoice")
                mark_unresolved(payment)
                return None
            if exc.issue in DEFINITE_REFUSALS:
                # No money moved. This request id now holds PayPal's refusal, so the next
                # attempt (after the buyer approves again) must use a new one.
                clear_charge_started(payment)
                self._remember(payment, capture_attempt=attempt + 1)
                return self._refused(payment, exc)
            # ORDER_ALREADY_CAPTURED, a request still in progress, rate limits, auth trouble or
            # anything unknown: what happened is whatever the order says now.
            logger.info(
                "paypal: capture of payment=%s answered %s; reading the order",
                payment.reference,
                exc.issue or exc.status,
            )
            order = self._get_order(order_id)
        return self._order_result(payment, order)

    @staticmethod
    def _remember(payment, **values) -> None:
        """Keep provider bookkeeping (capture attempt, hold reason) on the payment."""
        from apps.billing.models import BillingPayment

        raw = {**(payment.raw or {}), **values}
        BillingPayment.objects.filter(pk=payment.pk).update(raw=raw)
        payment.raw = raw

    def _refused(self, payment, exc) -> ProviderResult | None:
        if exc.issue == "ORDER_NOT_APPROVED":
            return None  # the buyer hasn't approved (yet)
        raw = {**(payment.raw or {}), "issue": exc.issue, "debug_id": exc.debug_id}
        if exc.issue in BUYER_CAN_RETRY:
            # PayPal: send the buyer back to approve again (e.g. pick another funding source).
            return ProviderResult(
                reference=payment.reference,
                status=REQUIRES_ACTION,
                action_url=self._approve_url(payment.provider_operation_id),
                failure_reason="PayPal declined the payment method; choose another one.",
                raw=raw,
            )
        logger.warning(
            "paypal: capture of payment=%s refused: %s debug_id=%s",
            payment.reference,
            exc.issue,
            exc.debug_id,
        )
        return ProviderResult(
            reference=payment.reference,
            status=FAILED,
            failure_reason=f"PayPal refused the payment ({exc.issue or exc.status}).",
            raw=raw,
        )

    def _approve_url(self, order_id: str) -> str:
        try:
            order = self._get_order(order_id)
        except ProviderError:
            order = {}
        link = _link(order, "payer-action") or _link(order, "approve")
        if link:
            return link
        host = (
            "www.sandbox.paypal.com"
            if config("PAYPAL_ENV") == "sandbox"
            else "www.paypal.com"
        )
        return f"https://{host}/checkoutnow?token={order_id}"

    @staticmethod
    def _client():
        try:
            return get_paypal_client()
        except PayPalError as exc:
            raise ProviderError(f"PayPal isn't configured: {exc}") from exc

    def _get_order(self, order_id: str) -> dict:
        try:
            return self._client().get_order(order_id)
        except PayPalError as exc:
            raise ProviderError(f"couldn't read order: {exc.issue or exc}") from exc

    def _order_result(self, payment, order: dict) -> ProviderResult | None:
        """A result for ``payment`` from its order, only if it really is this payment's money."""
        capture = _first_capture(order)
        if not capture:
            return None
        problems = []
        if order.get("id") != payment.provider_operation_id:
            problems.append("order id")
        payee = _payee(order)
        if payee and payee != config("PAYPAL_MERCHANT_ID"):
            problems.append("payee")
        if not payee:
            # A minimal capture response has no payee; read the order once to check it.
            full = self._get_order(payment.provider_operation_id)
            if _payee(full) != config("PAYPAL_MERCHANT_ID"):
                problems.append("payee")
        problems += self._identity_problems(payment, capture)
        if problems:
            logger.error(
                "paypal: rejected order=%s for payment=%s (%s mismatch)",
                order.get("id"),
                payment.reference,
                ", ".join(problems),
            )
            return None
        return self._capture_result(payment.reference, capture)

    @staticmethod
    def _identity_problems(payment, capture: dict) -> list[str]:
        problems = []
        if capture.get("custom_id") != payment.reference:
            problems.append("custom_id")
        if capture.get("invoice_id") != payment.reference:
            problems.append("invoice_id")
        return problems

    @staticmethod
    def _capture_result(reference: str, capture: dict) -> ProviderResult:
        status = capture.get("status") or ""
        amount = capture.get("amount") or {}
        reason = (capture.get("status_details") or {}).get("reason", "")
        mapped = CAPTURE_STATUS.get(status, PENDING)
        return ProviderResult(
            reference=reference,
            status=mapped,
            provider_ref=capture.get("id", ""),
            amount=to_decimal(amount.get("value")),
            currency=(amount.get("currency_code") or "").upper(),
            failure_reason=(reason or status)[:255] if mapped == FAILED else "",
            raw={"capture": capture.get("id"), "status": status, "reason": reason},
        )

    def reconcile(self, payment) -> ProviderResult | None:
        """What happened to a capture we never heard back about.

        An order with no capture was never charged (only we capture): the payment stops being
        in flight and is an ordinary unfinished checkout again, so the customer can pay (and it
        is abandoned in the usual way). Anything else is read from the capture itself. A payment
        held for a duplicate invoice id is the operator's to settle, never this order's."""
        from apps.billing.payments import clear_charge_started

        if not payment.provider_operation_id or (payment.raw or {}).get("hold"):
            return None
        order = self._get_order(payment.provider_operation_id)
        if order.get("id") != payment.provider_operation_id:
            return None
        if not _first_capture(order):
            if order.get("status") in UNCAPTURED_ORDER_STATUSES:
                clear_charge_started(payment)
                return ProviderResult(
                    reference=payment.reference,
                    status=PENDING,
                    raw={**(payment.raw or {}), "order_status": order.get("status")},
                )
            return None
        return self._order_result(payment, order)

    # --- webhooks --------------------------------------------------------------------------

    def parse_webhook(self, request) -> list[ProviderEvent]:
        webhook_id = config("PAYPAL_WEBHOOK_ID")
        if not webhook_id:
            raise InvalidSignature("PAYPAL_WEBHOOK_ID is not configured")
        try:
            event = json.loads(request.body)
        except (ValueError, TypeError) as exc:
            raise InvalidSignature("body is not JSON") from exc
        if not isinstance(event, dict) or not event.get("id"):
            raise InvalidSignature("not a PayPal event")
        payload = {
            key: request.headers.get(header, "")
            for key, header in TRANSMISSION_HEADERS.items()
        }
        if not all(payload.values()):
            raise InvalidSignature("missing PayPal transmission headers")
        payload["webhook_id"] = webhook_id
        try:
            # The event exactly as PayPal sent it: a re-serialised copy may not verify.
            verified = self._client().verify_webhook_signature(
                payload, request.body.decode("utf-8")
            )
        except PayPalUncertain as exc:
            raise ProviderError("couldn't verify the signature") from exc
        except PayPalError as exc:
            if exc.status >= 500 or not exc.status:
                raise ProviderError("couldn't verify the signature") from exc
            raise InvalidSignature(f"verification refused: {exc.issue}") from exc
        if not verified:
            raise InvalidSignature("signature did not verify")

        kind = event.get("event_type") or ""
        resource = event.get("resource") or {}
        key = f"paypal:{kind}:{event['id']}"
        if kind == "CHECKOUT.ORDER.APPROVED":
            # Moves money, so it runs as the event's handler: after the de-dupe check and in
            # the same transaction, never for a delivery that was already processed.
            order_id = resource.get("id")
            return [ProviderEvent(key=key, handler=lambda: self._approved(order_id))]
        if kind in CAPTURE_EVENTS:
            return [
                ProviderEvent(key=key, result=self._capture_event(resource.get("id")))
            ]
        if kind in REFUND_EVENTS:
            return [
                ProviderEvent(
                    key=key,
                    result=self._refund_event(resource.get("id"), REFUND_EVENTS[kind]),
                )
            ]
        return [ProviderEvent(key=key)]

    def _payment_for_order(self, order_id):
        from apps.billing.models import BillingPayment

        if not order_id:
            return None
        return BillingPayment.objects.filter(
            provider=self.code, provider_operation_id=order_id
        ).first()

    def _approved(self, order_id) -> None:
        """The buyer approved but may have closed the tab before returning: capture now."""
        from apps.billing.models import BillingPayment
        from apps.billing.payments import record_result

        payment = self._payment_for_order(order_id)
        if payment is None or payment.status == BillingPayment.SUCCEEDED:
            return
        result = self._capture(payment)
        if result is not None:
            record_result(self.code, result)

    def _capture_event(self, capture_id) -> ProviderResult | None:
        from apps.billing.models import BillingPayment

        if not capture_id:
            return None
        try:
            capture = self._client().get_capture(capture_id)
        except PayPalError as exc:
            raise ProviderError(f"couldn't read capture: {exc.issue or exc}") from exc
        reference = capture.get("custom_id") or ""
        payment = BillingPayment.objects.filter(
            provider=self.code, reference=reference
        ).first()
        if payment is None:
            logger.warning("paypal: capture %s isn't one of our payments", capture_id)
            return None
        order_id = (
            (capture.get("supplementary_data") or {}).get("related_ids") or {}
        ).get("order_id")
        problems = self._identity_problems(payment, capture)
        if order_id and order_id != payment.provider_operation_id:
            problems.append("order id")
        if problems:
            logger.error(
                "paypal: rejected capture=%s for payment=%s (%s mismatch)",
                capture_id,
                reference,
                ", ".join(problems),
            )
            return None
        return self._capture_result(reference, capture)

    def _refund_event(self, refund_id, kind) -> ProviderResult | None:
        if not refund_id:
            return None
        client = self._client()
        try:
            refund = client.get_refund(refund_id)
        except PayPalError as exc:
            raise ProviderError(f"couldn't read refund: {exc.issue or exc}") from exc
        status = refund.get("status")
        if status in ("FAILED", "CANCELLED"):
            logger.info("paypal: refund %s %s; nothing to record", refund_id, status)
            return None
        if status != "COMPLETED":
            # Not final yet (e.g. PENDING): answer 500 so PayPal delivers the event again later,
            # rather than marking it processed and never recording the refund.
            raise ProviderError(f"refund {refund_id} is {status}; retry later")
        reference = refund.get("custom_id") or refund.get("invoice_id") or ""
        if not reference:
            # Older refunds don't carry our ids; the capture they belong to does.
            capture_id = _link(refund, "up").rstrip("/").rsplit("/", 1)[-1]
            if capture_id:
                try:
                    reference = client.get_capture(capture_id).get("custom_id") or ""
                except PayPalError as exc:
                    raise ProviderError(
                        f"couldn't read capture: {exc.issue or exc}"
                    ) from exc
        if not reference:
            logger.warning(
                "paypal: refund %s isn't linked to one of our payments", refund_id
            )
            return None
        amount = refund.get("amount") or {}
        return ProviderResult(
            reference=reference,
            status=REFUNDED,
            currency=(amount.get("currency_code") or "").upper(),
            refund_id=refund_id,
            refund_amount=to_decimal(amount.get("value")),
            refund_kind=kind,
            raw={"refund": refund_id, "status": refund.get("status")},
        )

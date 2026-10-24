"""The contract every payment provider implements.

Akilent owns the subscription lifecycle (periods, credit, renewals, dunning, cancellation); a
provider only moves money. It does at most three things:

1. ``start_checkout``: send the customer to a hosted page to pay one ``BillingPayment`` (and save
   the payment method for later, where the provider supports it).
2. ``handle_return`` / ``parse_webhook``: report what happened, as provider-neutral
   ``ProviderResult``s keyed by our payment reference. The billing core
   (``apps.billing.payments.record_result``) decides what that means for the subscription.
3. ``charge_saved_method``: charge a saved method off-session for a renewal (optional).

Nothing outside ``apps.billing.providers`` knows a provider's API, field names or event types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

SUCCEEDED = "succeeded"
FAILED = "failed"
PENDING = "pending"
REQUIRES_ACTION = "requires_action"
# Money went back to the customer after the payment succeeded (a refund, reversal or
# chargeback). The payment stays paid; the refund is recorded beside it (payments.record_refund).
REFUNDED = "refunded"

REFUND = "refund"
REVERSAL = "reversal"
CHARGEBACK = "chargeback"


class ProviderError(Exception):
    """The provider couldn't do what was asked; the message is safe to show an operator."""


class InvalidSignature(ProviderError):
    """A webhook didn't carry a valid signature for this provider."""


@dataclass
class SavedMethodData:
    token: str
    customer_ref: str = ""
    brand: str = ""
    last4: str = ""
    expiry: str = ""
    # What the customer sees for a method that isn't a card, e.g. "PayPal · j***@x.com".
    label: str = ""
    # Which merchant account the token belongs to (PaymentProvider.account_key).
    provider_account: str = ""


@dataclass
class ProviderResult:
    """What a provider says happened to one of our payments. Amounts are in major units.

    A ``REFUNDED`` result describes one refund of a paid payment: ``refund_id`` identifies it
    (so a replay is recorded once), ``refund_amount`` is that refund alone, and
    ``refund_kind`` is REFUND, REVERSAL or CHARGEBACK."""

    reference: str
    status: str
    provider_ref: str = ""
    amount: Decimal | None = None
    currency: str = ""
    saved_method: SavedMethodData | None = None
    action_url: str = ""
    failure_reason: str = ""
    raw: dict = field(default_factory=dict)
    refund_id: str = ""
    refund_amount: Decimal | None = None
    refund_kind: str = REFUND


@dataclass
class ProviderEvent:
    """One webhook event. ``key`` de-duplicates retries. ``result`` (if any) goes to the billing
    core; ``handler`` (if any) is provider-specific work, such as winding down a legacy
    provider-side subscription, run in the same transaction."""

    key: str
    result: ProviderResult | None = None
    handler: Callable[[], None] | None = None


class PaymentProvider(ABC):
    code: str = ""
    display_name: str = ""
    # The checkout button for this provider.
    checkout_label: str = "Pay by card"
    # Does a return in REQUIRES_ACTION send the customer straight to ``action_url`` (PayPal:
    # approve again after a decline)? Otherwise they see the pending page.
    resume_on_return: bool = False
    # Can the customer's method be saved at checkout and charged again without them?
    off_session_charge: bool = False
    # Can the operator make this the active provider (manual bank transfer can't take renewals
    # on its own, so it's an extra option rather than the provider).
    selectable: bool = True

    # ISO codes this provider can charge; empty means any. A property so a provider can read
    # it from settings (PayPal); a plain class attribute overrides it too.
    @property
    def supported_currencies(self) -> frozenset:
        return frozenset()

    # Can it look up a charge whose outcome we never learned (``reconcile``)? Without it, an
    # unconfirmed payment is given up after a day, as before.
    can_reconcile: bool = False
    # Does this provider receive webhooks at /billing/webhooks/<code>/?
    has_webhooks: bool = True

    def is_configured(self) -> bool:
        """Are the provider's server settings (keys, secrets) present?"""
        return True

    def supports_currency(self, currency: str) -> bool:
        return (
            not self.supported_currencies
            or currency.upper() in self.supported_currencies
        )

    @abstractmethod
    def start_checkout(self, request, payment):
        """Return the HttpResponse that starts paying ``payment.amount_due`` (usually a redirect
        to the provider's hosted page, returning to ``return_url(request, payment)``)."""

    def reference_from_return(self, request) -> str | None:
        """Our payment reference from the customer's return to /billing/return/<code>/."""
        return request.GET.get("ref") or None

    def handle_return(self, request, payment) -> ProviderResult | None:
        """Verify the customer's return with the provider. The redirect alone is never proof of
        payment; return None when there is nothing verifiable yet."""
        return None

    def parse_webhook(self, request) -> list[ProviderEvent]:
        """Verify the signature (raise InvalidSignature) and translate the body into events."""
        raise InvalidSignature(f"{self.code} does not receive webhooks")

    def charge_saved_method(self, method, payment) -> ProviderResult:
        raise ProviderError(f"{self.display_name} can't charge a saved payment method")

    def account_key(self) -> str:
        """The merchant account saved methods belong to, e.g. "live:MERCHANTID". A method saved
        under one key is never charged under another (switching sandbox to live, or to a
        different merchant account). Empty means the provider doesn't distinguish."""
        return ""

    def reconcile(self, payment) -> ProviderResult | None:
        """Find out what happened to a charge whose outcome we never learned (a timeout after
        the request was sent). Must not start a new charge. None means still unknown."""
        return None

    def cancel_legacy(self, subscription) -> SavedMethodData | None:
        """Stop a provider-side recurring subscription created before Akilent ran renewals
        itself. Returns the saved method behind it, when the provider can tell us."""
        return None

    def config_status(self) -> str:
        """One line for the operator console: what's missing, or "" when ready."""
        return "" if self.is_configured() else "Server keys are not set."


def return_url(request, payment) -> str:
    from django.urls import reverse

    path = reverse("billing:return", args=[payment.provider])
    return request.build_absolute_uri(f"{path}?ref={payment.reference}")


def payment_label(payment) -> str:
    """What the customer sees on the provider's page, e.g. "Starter (Quarterly)"."""
    from apps.billing.models import Subscription

    period = dict(Subscription.BILLING_PERIOD_CHOICES).get(payment.billing_period, "")
    return f"{payment.plan.name} ({period})" if period else payment.plan.name

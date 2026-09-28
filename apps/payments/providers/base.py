"""Provider-agnostic PaymentProvider interface.

Any payment processor backend (Stripe, Square, PayPal, bank transfer, ...)
is supported by implementing this ABC. Business logic imports only from here
and from apps.payments.types — never from a concrete provider module.

Design principle: every method returns a typed dataclass from apps.payments.types,
never a raw dict. Adapters belong in the provider, not scattered across the
service layer.

PHASE 3 STUB: This file defines the interface but has no concrete implementations yet.
Payment execution will be added in a later phase once transaction module needs it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ChargeResult:
    """Result of a payment charge operation."""

    transaction_id: str
    """Unique ID for this charge from the payment processor."""

    success: bool
    """Whether the charge was accepted and will proceed."""

    amount_cents: int
    """Amount charged in cents."""

    error: str | None = None
    """Error message if success=False."""

    metadata: dict[str, object] | None = None
    """Extra data from the provider (e.g., receipt URL, risk score)."""

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class RefundResult:
    """Result of a refund operation."""

    refund_id: str
    """Unique ID for this refund from the payment processor."""

    success: bool
    """Whether the refund was accepted."""

    amount_cents: int
    """Amount refunded in cents."""

    error: str | None = None
    """Error message if success=False."""


@dataclass
class TransactionStatusResult:
    """Result of checking a transaction's status."""

    status: str
    """'pending' | 'completed' | 'failed' | 'refunded' | 'disputed'."""

    amount_cents: int
    """Original transaction amount in cents."""

    error: str | None = None
    """Error message if status could not be determined."""


class PaymentProvider(ABC):
    """Abstract interface for a payment processing backend.

    Implement all abstract methods to add a new provider. The factory in
    apps.payments.providers.__init__ will resolve the concrete class at runtime.

    Threading: instances are not thread-safe. Instantiate one per request,
    per Celery task, or per service call.

    PHASE 3 STUB: This interface is defined but has no implementations.
    Phase 5 will add concrete implementations (Stripe, Square, etc.).
    """

    @abstractmethod
    def charge(
        self,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        description: str = "",
        metadata: dict[str, object] | None = None,
    ) -> ChargeResult:
        """Charge a payment method.

        Args:
            amount_cents: Amount to charge in cents (e.g., 9999 = $99.99).
            currency: ISO 4217 code ('usd', 'eur', etc.).
            idempotency_key: Unique key to prevent duplicate charges if retried.
            description: Human-readable reason for the charge.
            metadata: Extra data to attach to the charge (business-specific).

        Returns:
            ChargeResult with transaction_id and success status.

        Raises:
            PaymentProviderError: on network failure, invalid card, etc.
        """

    @abstractmethod
    def refund(
        self, transaction_id: str, amount_cents: int | None = None
    ) -> RefundResult:
        """Refund a previous charge.

        Args:
            transaction_id: ID from ChargeResult of the original charge.
            amount_cents: Amount to refund. If None, refund full amount.

        Returns:
            RefundResult with refund_id and success status.

        Raises:
            PaymentProviderError: on network failure, transaction not found, etc.
        """

    @abstractmethod
    def get_status(self, transaction_id: str) -> TransactionStatusResult:
        """Check the status of a transaction.

        Args:
            transaction_id: ID from ChargeResult.

        Returns:
            TransactionStatusResult with current status.

        Raises:
            PaymentProviderError: on network failure, transaction not found, etc.
        """


class PaymentProviderError(Exception):
    """Base exception for payment provider errors."""

    pass

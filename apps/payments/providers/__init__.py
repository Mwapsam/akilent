"""Payment provider factory.

PHASE 3 STUB: This factory is defined but no concrete implementations exist yet.
Phase 5 will add actual payment processors (Stripe, Square, PayPal, etc.).

Usage (Phase 5+):
    from apps.payments.providers import get_payment_provider
    provider = get_payment_provider(account)
    result = provider.charge(amount_cents=9999, currency='usd', idempotency_key=...)
"""

import logging

logger = logging.getLogger(__name__)

from apps.payments.providers.base import PaymentProvider, PaymentProviderError

_ALIASES: dict[str, str] = {
    # Phase 5: add implementations
    # "stripe": "apps.payments.providers.stripe.StripeProvider",
}


def get_payment_provider(account) -> PaymentProvider:
    """Return a PaymentProvider instance for the given account.

    Args:
        account: An apps.accounts.Account instance.

    Returns:
        A PaymentProvider implementation.

    Raises:
        PaymentProviderError: if no provider is configured.

    PHASE 3 STUB: No implementations exist yet.
    """
    raise PaymentProviderError(
        "Payment provider not configured. Phase 5 will add Stripe/Square/PayPal implementations."
    )


__all__ = [
    "get_payment_provider",
    "PaymentProvider",
    "PaymentProviderError",
]

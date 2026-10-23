"""The platform billing currency and amount conversions shared by every payment provider.

Plan prices and credits are all in one currency (``settings.BILLING_CURRENCY``). Providers get
amounts in major units (19.00) and convert to their own wire format here, so a zero-decimal
currency (e.g. UGX, JPY) is never multiplied by 100.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings

# ISO 4217 currencies with no minor unit (Stripe's list, plus the African ones Flutterwave settles).
ZERO_DECIMAL = frozenset(
    {
        "BIF", "CLP", "DJF", "GNF", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND",
        "VUV", "XAF", "XOF", "XPF",
    }
)  # fmt: skip

SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "NGN": "₦",
    "KES": "KSh ",
    "ZMW": "K",
    "GHS": "GH₵",
    "ZAR": "R",
    "UGX": "USh ",
    "TZS": "TSh ",
    "RWF": "RF ",
}


def billing_currency() -> str:
    return (getattr(settings, "BILLING_CURRENCY", "") or "USD").upper()


def to_decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def quantize(amount: Decimal) -> Decimal:
    return Decimal(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def to_minor_units(amount: Decimal, currency: str) -> int:
    amount = Decimal(amount)
    if currency.upper() in ZERO_DECIMAL:
        return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor_units(value: int, currency: str) -> Decimal:
    if currency.upper() in ZERO_DECIMAL:
        return Decimal(int(value))
    return (Decimal(int(value)) / 100).quantize(Decimal("0.01"))


def covers(paid, paid_currency: str, expected: Decimal, expected_currency: str) -> bool:
    """True if ``paid`` in ``paid_currency`` is at least ``expected`` in the same currency.
    A different currency never covers - amounts in two currencies aren't comparable."""
    amount = to_decimal(paid)
    if amount is None or (paid_currency or "").upper() != expected_currency.upper():
        return False
    return amount >= Decimal(expected)


def format_money(amount, currency: str | None = None) -> str:
    currency = (currency or billing_currency()).upper()
    value = to_decimal(amount) or Decimal("0")
    symbol = SYMBOLS.get(currency)
    number = f"{value:,.0f}" if currency in ZERO_DECIMAL else f"{value:,.2f}"
    return f"{symbol}{number}" if symbol else f"{number} {currency}"

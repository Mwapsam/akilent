"""Deterministic buying-intent signals read off a customer's message.

Pre-AI by design (see docs/plans — "AI enhances Akilent's automation, it does
not define it"): no model, no scoring, just phrases a business would recognise
as "this person is asking to buy something". When AI lands it replaces
``detect_buying_intent`` and everything downstream keeps working, because the
rest of the system consumes the *signal*, not the way it was derived.

Deliberately conservative. A false positive creates a lead a human then has to
dismiss, so the phrases here are ones that rarely appear in small talk.
"""
from __future__ import annotations

import re

# Grouped only for readability — all are treated the same.
_INTENT_PHRASES = [
    # Price
    "how much", "how much is", "what is the price", "what's the price", "price",
    "cost", "how many kwacha", "quote", "quotation", "discount",
    # Availability
    "do you have", "is it available", "in stock", "still available", "available",
    # Purchase
    "i want to buy", "i want", "i need", "can i order", "i would like to order",
    "i'd like to order", "order", "buy", "purchase", "book", "reserve",
    # Fulfilment (asked before buying, not after)
    "do you deliver", "delivery", "how soon can", "when can i get",
]

# Word-boundary match so "order" doesn't fire on "in order to" — checked below —
# and "price" doesn't fire inside "priceless".
_PATTERN = re.compile(
    r"(?<!\w)(" + "|".join(re.escape(p) for p in _INTENT_PHRASES) + r")(?!\w)",
    re.IGNORECASE,
)

# Phrases that contain an intent word but aren't buying intent.
_EXCLUSIONS = re.compile(
    r"in order to|order of (?:the )?(?:day|service)|no thanks|just (?:looking|browsing)",
    re.IGNORECASE,
)


def detect_buying_intent(body: str) -> str | None:
    """Return the phrase that signalled buying intent, or ``None``.

    Returning the matched phrase rather than a bare bool so the lead can record
    *why* it was created — a business owner who sees an unexpected lead needs to
    be able to tell what triggered it.
    """
    if not body or not body.strip():
        return None
    if _EXCLUSIONS.search(body):
        return None
    match = _PATTERN.search(body)
    return match.group(1).lower() if match else None

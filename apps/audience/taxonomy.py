"""The fixed Audience Intelligence taxonomy (docs/plans/audience-intelligence-spec.md §3).

Owners see "What people are saying", never "sentiment" or a score. Each primary
category maps to something the owner can do.
"""

from __future__ import annotations

from django.db import models


class Category(models.TextChoices):
    BUYING_INTENT = "buying_intent", "Buying intent"
    PRODUCT_ENQUIRY = "product_enquiry", "Product question"
    COMPLAINT = "complaint", "Complaint"
    PRAISE = "praise", "Praise"
    CONTENT_FEEDBACK = "content_feedback", "Content feedback"
    SPAM_ABUSE = "spam_abuse", "Spam or abuse"
    GENERAL = "general", "General"


SUBCATEGORIES: dict[str, tuple[str, ...]] = {
    Category.BUYING_INTENT: (
        "price",
        "purchase",
        "availability",
        "ordering",
        "dm_request",
    ),
    Category.PRODUCT_ENQUIRY: (
        "size",
        "colour",
        "specs",
        "stock",
        "delivery",
        "location",
    ),
    Category.COMPLAINT: (
        "quality",
        "delivery_delay",
        "pricing",
        "service",
        "dissatisfaction",
    ),
    Category.PRAISE: ("product", "brand", "satisfaction"),
    Category.CONTENT_FEEDBACK: ("appreciation", "criticism", "more_content"),
    Category.SPAM_ABUSE: ("spam", "promo", "toxic"),
    Category.GENERAL: ("greeting", "chit_chat", "other"),
}

# Which category is "primary" when a comment carries several. Used only for
# single-bucket views; per-category counts always use every signal.
PRECEDENCE: tuple[str, ...] = (
    Category.BUYING_INTENT,
    Category.COMPLAINT,
    Category.PRODUCT_ENQUIRY,
    Category.CONTENT_FEEDBACK,
    Category.PRAISE,
    Category.SPAM_ABUSE,
    Category.GENERAL,
)


def is_valid(category: str, subcategory: str = "") -> bool:
    if category not in SUBCATEGORIES:
        return False
    return not subcategory or subcategory in SUBCATEGORIES[category]


def precedence_rank(category: str) -> int:
    try:
        return PRECEDENCE.index(category)
    except ValueError:
        return len(PRECEDENCE)

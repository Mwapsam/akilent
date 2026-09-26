"""The business profile: the answers to "tell us about your business", and what they're used for.

One place reads and writes it so every consumer sees the same wording: automation reply text and
template blanks (``business.location``...), AI's structured facts, and the notes AI starts from.
Answers are optional; an unanswered one is simply absent, never guessed.
"""
from __future__ import annotations

from django.utils import timezone

PAYMENT_METHODS = {
    "cash": "Cash",
    "mtn_momo": "MTN MoMo",
    "airtel_money": "Airtel Money",
    "bank_transfer": "Bank transfer",
    "card": "Card",
}
MAX_TEXT = 300


class ProfileError(ValueError):
    """A problem with submitted answers, worded for a business owner."""


def get_profile(account):
    from apps.accounts.models import BusinessProfile

    return BusinessProfile.objects.filter(account=account).first()


def save_profile(account, *, what_you_sell="", location="", delivers=None, delivery_notes="",
                 payment_methods=(), payment_other="", website=""):
    from django.core.exceptions import ValidationError
    from django.core.validators import URLValidator

    from apps.accounts.models import BusinessProfile

    website = (website or "").strip()
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    if website:
        try:
            URLValidator()(website)
        except ValidationError:
            raise ProfileError("Enter your website like example.com.") from None
    profile, _ = BusinessProfile.objects.get_or_create(account=account)
    profile.what_you_sell = (what_you_sell or "").strip()[:MAX_TEXT]
    profile.location = (location or "").strip()[:MAX_TEXT]
    profile.delivers = delivers
    profile.delivery_notes = (delivery_notes or "").strip()[:MAX_TEXT] if delivers else ""
    profile.payment_methods = [m for m in PAYMENT_METHODS if m in set(payment_methods or ())]
    profile.payment_other = (payment_other or "").strip()[:120]
    profile.website = website
    profile.completed_at = profile.completed_at or timezone.now()
    profile.save()
    return profile


def payment_text(profile) -> str:
    """"MTN MoMo, Airtel Money or Cash", or "" when not answered."""
    if profile is None:
        return ""
    names = [PAYMENT_METHODS[m] for m in profile.payment_methods or [] if m in PAYMENT_METHODS]
    if profile.payment_other:
        names.append(profile.payment_other)
    if not names:
        return ""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def delivery_text(profile) -> str:
    if profile is None or profile.delivers is None:
        return ""
    if not profile.delivers:
        return "We don't deliver; customers collect."
    return f"We deliver. {profile.delivery_notes}".strip() if profile.delivery_notes else "We deliver."


def value(account, key: str) -> str | None:
    """One answer as text, for a template blank or reply (``business.<key>``). None if unanswered."""
    if key == "opening_hours":
        from apps.accounts import business_hours

        return business_hours.describe(account) or None
    profile = get_profile(account)
    if profile is None:
        return None
    text = {
        "location": profile.location, "website": profile.website, "what_you_sell": profile.what_you_sell,
        "payment_methods": payment_text(profile), "delivery": delivery_text(profile),
    }.get(key, "")
    return text or None


def as_facts(account) -> dict:
    """The answered parts only, for AI's structured facts."""
    profile = get_profile(account)
    if profile is None:
        return {}
    facts = {
        "what_you_sell": profile.what_you_sell, "location": profile.location,
        "payment_methods": payment_text(profile), "delivery": delivery_text(profile), "website": profile.website,
    }
    return {k: v for k, v in facts.items() if v}


def as_notes(account) -> str:
    """The answers as a readable starting point for the AI notes the owner can then edit."""
    labels = (("what_you_sell", "What we sell"), ("location", "Where we are"), ("delivery", "Delivery"),
              ("payment_methods", "Payment"), ("website", "Website"))
    facts = as_facts(account)
    return "\n".join(f"{label}: {facts[key]}" for key, label in labels if facts.get(key))

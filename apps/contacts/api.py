"""Public API of the contacts module for other apps (billing counts customers for plan limits)."""

from __future__ import annotations


def count_contacts(account) -> int:
    """Customers stored for this business."""
    from apps.contacts.models import Contact

    return Contact.objects.filter(account=account).count()


def contacts_with_phone(account_id: int | None = None):
    """Customers that have a phone number (all businesses, or one), oldest first.

    For WhatsApp's identity backfill: anyone here can be reached on WhatsApp.
    """
    from apps.contacts.models import Contact

    qs = Contact.objects.exclude(phone__isnull=True).exclude(phone="").order_by("pk")
    if account_id:
        qs = qs.filter(account_id=account_id)
    return qs

"""Public API of the contacts module for other apps (billing counts customers for plan limits)."""

from __future__ import annotations


def count_contacts(account) -> int:
    """Customers stored for this business."""
    from apps.contacts.models import Contact

    return Contact.objects.filter(account=account).count()

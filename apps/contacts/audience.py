"""Turn a ContactList or Segment into campaign recipient rows.

Personalization variables for each recipient are the contact's own fields plus
its ``attributes`` — so a template can use ``{{ first_name }}`` or
``{{ attributes.plan }}``.
"""
from __future__ import annotations

from apps.contacts.models import Contact, ContactList, Segment
from apps.contacts.segments import contacts_for

_SENDABLE = {Contact.Status.SUBSCRIBED}


def _variables(contact: Contact) -> dict:
    return {
        "email": contact.email,
        "first_name": contact.first_name,
        "last_name": contact.last_name,
        "full_name": contact.full_name,
        "attributes": contact.attributes or {},
    }


def resolve_recipients(account, *, list_slug: str | None = None, segment_slug: str | None = None) -> list[dict]:
    """Return ``[{"to": email, "variables": {...}}]`` for a list or segment.

    Only ``subscribed`` contacts are included.
    """
    if list_slug:
        try:
            lst = ContactList.objects.get(account=account, slug=list_slug)
        except ContactList.DoesNotExist as exc:
            raise ValueError(f"list {list_slug!r} not found") from exc
        qs = lst.contacts.filter(account=account)
    elif segment_slug:
        try:
            seg = Segment.objects.get(account=account, slug=segment_slug)
        except Segment.DoesNotExist as exc:
            raise ValueError(f"segment {segment_slug!r} not found") from exc
        qs = contacts_for(seg.definition, account)
    else:
        raise ValueError("provide list_slug or segment_slug")

    return [
        {"to": c.email, "variables": _variables(c)}
        for c in qs.filter(status__in=_SENDABLE).iterator()
    ]

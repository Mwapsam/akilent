from __future__ import annotations

import logging

from apps.instagram.models.account import InstagramBusinessAccount
from apps.instagram.models.contact import InstagramContact

logger = logging.getLogger(__name__)


def resolve_or_create_contact(
    instagram_account: InstagramBusinessAccount,
    igsid: str,
    *,
    username: str = "",
    name: str = "",
) -> InstagramContact:
    """
    Get or create an InstagramContact for the given IGSID, then ensure it has
    a linked apps.contacts.Contact.

    The InstagramContact is the channel identity; the linked Contact is the
    canonical identity that leads, conversations, and the rest of Akilent use.
    """
    ig_contact, created = InstagramContact.objects.get_or_create(
        account=instagram_account.account,
        instagram_scoped_id=igsid,
        defaults={
            "username": username,
            "name": name,
        },
    )

    if not created and (username or name):
        # Update display fields if they've changed
        updated_fields = []
        if username and ig_contact.username != username:
            ig_contact.username = username
            updated_fields.append("username")
        if name and ig_contact.name != name:
            ig_contact.name = name
            updated_fields.append("name")
        if updated_fields:
            ig_contact.save(update_fields=updated_fields)

    if ig_contact.contact_id is None:
        _link_canonical_contact(ig_contact)

    return ig_contact


def _link_canonical_contact(ig_contact: InstagramContact) -> None:
    """Create a new apps.contacts.Contact and link it to this InstagramContact."""
    from apps.contacts.models import Contact

    contact = Contact.objects.create(
        account=ig_contact.account,
        source="instagram",
    )
    ig_contact.contact = contact
    ig_contact.save(update_fields=["contact"])
    logger.debug(
        "Created Contact %s for InstagramContact %s",
        contact.pk,
        ig_contact.instagram_scoped_id,
    )

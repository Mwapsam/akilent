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

    # Enrich with IG profile if we still have no name/username
    if not ig_contact.username and not ig_contact.name:
        _enrich_contact_profile(ig_contact, instagram_account)

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


def _enrich_contact_profile(
    ig_contact: InstagramContact,
    instagram_account: InstagramBusinessAccount,
) -> None:
    """Fetch name and username from the Graph API and save them on the contact.

    Best-effort — failures are logged and silently swallowed so they never
    block message processing.
    """
    import requests
    from django.conf import settings

    access_token = instagram_account.access_token
    if not access_token:
        return
    version = getattr(settings, "INSTAGRAM_GRAPH_VERSION", "v21.0")
    try:
        resp = requests.get(
            f"https://graph.instagram.com/{version}/{ig_contact.instagram_scoped_id}",
            params={"fields": "name,username", "access_token": access_token},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.debug(
                "_enrich_contact_profile: %s status=%s",
                ig_contact.instagram_scoped_id,
                resp.status_code,
            )
            return
        data = resp.json()
        updated_fields = []
        if data.get("name") and not ig_contact.name:
            ig_contact.name = data["name"]
            updated_fields.append("name")
        if data.get("username") and not ig_contact.username:
            ig_contact.username = data["username"]
            updated_fields.append("username")
        if updated_fields:
            ig_contact.save(update_fields=updated_fields)
            # Mirror onto the canonical Contact's first/last name
            if ig_contact.contact_id:
                _sync_name_to_contact(ig_contact)
    except Exception:
        logger.debug(
            "_enrich_contact_profile: failed for %s",
            ig_contact.instagram_scoped_id,
            exc_info=True,
        )


def _sync_name_to_contact(ig_contact: InstagramContact) -> None:
    """Copy IG name → Contact.first_name if it's still blank."""
    from apps.contacts.models import Contact

    try:
        if ig_contact.contact_id is None:
            return
        contact = Contact.objects.get(pk=ig_contact.contact_id)
        if not contact.first_name and ig_contact.name:
            parts = ig_contact.name.split(maxsplit=1)
            contact.first_name = parts[0]
            contact.last_name = parts[1] if len(parts) > 1 else ""
            contact.save(update_fields=["first_name", "last_name"])
    except Contact.DoesNotExist:
        pass

from __future__ import annotations

import logging

from apps.facebook.models.contact import FacebookContact

logger = logging.getLogger(__name__)


def resolve_or_create_contact(conn, psid: str, *, name: str = "") -> FacebookContact:
    """
    Get or create the FacebookContact for a PSID on the connected Page ``conn``, then
    make sure it is linked to a canonical apps.contacts.Contact.

    Mirrors apps.instagram.services.contacts.resolve_or_create_contact.
    """
    fb_contact, created = FacebookContact.objects.get_or_create(
        account=conn.account,
        psid=psid,
        defaults={"name": name},
    )
    if not created and name and fb_contact.name != name:
        fb_contact.name = name
        fb_contact.save(update_fields=["name"])

    if fb_contact.contact_id is None:
        _link_canonical_contact(fb_contact)

    # A Messenger webhook carries no name, so look the person up once.
    if not fb_contact.name:
        _enrich_contact_profile(fb_contact, conn)

    return fb_contact


def _link_canonical_contact(fb_contact: FacebookContact) -> None:
    """Create a canonical Contact under a row lock (see the Instagram version for why)."""
    from django.db import transaction

    from apps.contacts.models import Contact

    with transaction.atomic():
        locked = FacebookContact.objects.select_for_update().get(pk=fb_contact.pk)
        if locked.contact_id is not None:
            fb_contact.contact_id = locked.contact_id
            return
        first, _, last = (locked.name or "").strip().partition(" ")
        contact = Contact.objects.create(
            account=locked.account,
            source="facebook",
            first_name=first[:150],
            last_name=last.strip()[:150],
        )
        locked.contact = contact
        locked.save(update_fields=["contact"])
    fb_contact.contact = contact


def _enrich_contact_profile(fb_contact: FacebookContact, conn) -> None:
    """Fetch the person's name and picture (Business Asset User Profile Access).

    Best effort: a failure never blocks message processing.
    """
    from apps.facebook.providers.meta import provider_for

    if not conn.access_token:
        return
    data = provider_for(conn).get_user_profile(fb_contact.psid)
    name = (
        data.get("name")
        or " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p)
    ).strip()
    updated = []
    if name and not fb_contact.name:
        fb_contact.name = name
        updated.append("name")
    if data.get("profile_pic") and not fb_contact.profile_pic_url:
        fb_contact.profile_pic_url = data["profile_pic"]
        updated.append("profile_pic_url")
    if not updated:
        return
    fb_contact.save(update_fields=updated)
    if "name" in updated:
        _sync_name_to_contact(fb_contact)


def _sync_name_to_contact(fb_contact: FacebookContact) -> None:
    """Copy the Facebook name onto the canonical Contact while its name is blank."""
    from apps.contacts.models import Contact

    if fb_contact.contact_id is None:
        return
    contact = Contact.objects.filter(pk=fb_contact.contact_id).first()
    if contact is None or contact.first_name or not fb_contact.name:
        return
    parts = fb_contact.name.split(maxsplit=1)
    contact.first_name = parts[0][:150]
    contact.last_name = (parts[1] if len(parts) > 1 else "")[:150]
    contact.save(update_fields=["first_name", "last_name"])

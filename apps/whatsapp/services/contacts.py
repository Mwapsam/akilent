"""WhatsApp contact resolution — canonical Contact linkage.

Mirrors apps/instagram/services/contacts.py: ensures every WhatsAppContact
has a linked canonical contacts.Contact before any spine work begins.
"""

from __future__ import annotations

from apps.whatsapp.models import WhatsAppContact


def resolve_channel_contact(account, wa_contact: WhatsAppContact):
    """Return the canonical Contact for a WhatsAppContact, creating one if absent.

    Matches the Instagram pattern:
      - existing contact.contact → return it unchanged
      - no contact → upsert by phone → link → return
    """
    from apps.contacts.services import upsert_contact_by_phone

    contact = wa_contact.contact
    if contact is None:
        contact, created = upsert_contact_by_phone(
            account, wa_contact.phone_number, source="whatsapp"
        )
        if created and wa_contact.display_name and not contact.first_name:
            contact.first_name = wa_contact.display_name[:150]
            contact.save(update_fields=["first_name", "updated_at"])
        wa_contact.contact = contact
        wa_contact.save(update_fields=["contact"])
    return contact

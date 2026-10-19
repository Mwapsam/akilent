"""WhatsApp contact resolution — canonical Contact linkage.

Mirrors apps/instagram/services/contacts.py: ensures every WhatsAppContact
has a linked canonical contacts.Contact before any spine work begins.
"""

from __future__ import annotations

import logging

from apps.whatsapp.models import WhatsAppContact

logger = logging.getLogger(__name__)


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


def ensure_whatsapp_identity(contact):
    """Make sure a Contact with a phone number can be messaged on WhatsApp.

    A ``WhatsAppContact`` used to exist only for people who had messaged the
    business (or been sent to by a workflow), so a customer added by hand,
    imported or put on a list was skipped by every campaign as "no WhatsApp
    number". This is the one place that closes that gap.

    Returns the linked WhatsAppContact, or None when the contact has no usable
    phone. A new identity starts in the UNKNOWN opt-in state — consent is never
    implied by having a number. An existing unlinked identity for the same
    number is linked to this contact; one already linked to a *different*
    contact is left alone (that's a duplicate to merge, not ours to steal).
    """
    from django.core.exceptions import ValidationError
    from django.db import IntegrityError, transaction

    from apps.whatsapp.models.contact import normalize_phone

    if not contact.phone:
        return None
    try:
        phone = normalize_phone(contact.phone)
    except ValidationError:
        return None

    linked = WhatsAppContact.objects.filter(
        account_id=contact.account_id, contact=contact
    ).first()
    if linked is not None and linked.phone_number == phone:
        return linked

    existing = WhatsAppContact.objects.filter(
        account_id=contact.account_id, phone_number=phone
    ).first()
    if existing is None:
        try:
            with transaction.atomic():
                return WhatsAppContact.objects.create(
                    account_id=contact.account_id, phone_number=phone, contact=contact
                )
        except IntegrityError:
            # A concurrent create for the same number won; fall through and link it.
            existing = WhatsAppContact.objects.filter(
                account_id=contact.account_id, phone_number=phone
            ).first()
            if existing is None:
                raise
    if existing.contact_id is None:
        existing.contact = contact
        existing.save(update_fields=["contact"])
    elif existing.contact_id != contact.pk:
        logger.info(
            "ensure_whatsapp_identity: %s already belongs to contact %s, not %s",
            phone,
            existing.contact_id,
            contact.pk,
        )
    return existing

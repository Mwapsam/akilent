"""WhatsApp signal handlers."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def on_contact_saved(sender, instance, created, update_fields=None, **kwargs):
    """Give every customer with a phone number a WhatsApp identity.

    Campaigns resolve recipients through ``WhatsAppContact``, so a customer added
    by hand, imported, synced from HubSpot or created through the API must get
    one too — otherwise they're silently skipped as "no WhatsApp number". Runs on
    create and whenever the phone may have changed, after the transaction
    commits (so code that creates the identity itself in the same transaction
    is linked, not duplicated), and never breaks the save.
    """
    if not instance.phone:
        return
    if not created and update_fields is not None and "phone" not in update_fields:
        return

    from django.db import transaction

    contact_id = instance.pk

    def _ensure():
        try:
            from apps.contacts.models import Contact
            from apps.whatsapp.services.contacts import ensure_whatsapp_identity

            contact = Contact.objects.filter(pk=contact_id).first()
            if contact is not None:
                ensure_whatsapp_identity(contact)
        except Exception:
            logger.exception(
                "whatsapp: ensure_whatsapp_identity failed for contact %s", contact_id
            )

    transaction.on_commit(_ensure)

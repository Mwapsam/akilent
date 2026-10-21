"""Delete a customer and everything about them — the one place it happens.

Used by the profile's "Delete customer", bulk delete on the Customers page,
``DELETE /v1/contacts/<id>`` and the operator console. Backs the promise on
/data-deletion/.

Deleting the Contact cascades to its spine conversations and messages, orders,
leads, deals, notes, list memberships and automation runs. On top of that this
removes what only *points* at the customer (and so would otherwise be left
behind with the link cleared): their WhatsApp identity with its message log
and send queue, and their Instagram identity with its comments, DMs and
queued replies. A connected HubSpot gets the contact archived too.
"""

from __future__ import annotations

from django.apps import apps
from django.db import transaction
from django.db.models import Q


def _whatsapp_identities(contact):
    WhatsAppContact = apps.get_model("whatsapp", "WhatsAppContact")
    q = Q(contact=contact)
    if contact.phone:
        q |= Q(phone_number=contact.phone, contact__isnull=True)
    return WhatsAppContact.objects.filter(account_id=contact.account_id).filter(q)


def _instagram_identities(contact):
    InstagramContact = apps.get_model("instagram", "InstagramContact")
    return InstagramContact.objects.filter(
        account_id=contact.account_id, contact=contact
    )


def preview(contact) -> list[tuple[str, int]]:
    """What deleting ``contact`` removes, as ``[(label, count)]`` for the confirm screen."""
    Message = apps.get_model("conversations", "Message")
    rows = [
        ("Conversations", contact.conversations.count()),
        ("Messages", Message.objects.filter(conversation__contact=contact).count()),
        ("Orders", contact.orders.count()),
        ("Leads", contact.leads.count()),
        ("Deals", contact.deals.count()),
        ("Notes", contact.notes.count()),
        ("WhatsApp numbers", _whatsapp_identities(contact).count()),
        ("Instagram accounts", _instagram_identities(contact).count()),
    ]
    return [(label, n) for label, n in rows if n]


def delete_customer(contact, *, actor=None, source: str = "ui") -> dict:
    """Permanently delete ``contact`` and everything about them. Returns counts."""
    InstagramOutbound = apps.get_model("instagram", "OutboundMessage")

    link = _hubspot_link(contact)
    hs_object_id = link.hs_object_id if link and not link.remote_archived else ""

    with transaction.atomic():
        wa = list(_whatsapp_identities(contact))
        ig = list(_instagram_identities(contact))
        igsids = [i.instagram_scoped_id for i in ig]
        if igsids:
            InstagramOutbound.objects.filter(
                instagram_account__account_id=contact.account_id,
                recipient_igsid__in=igsids,
            ).delete()
        for identity in [*wa, *ig]:
            identity.delete()
        account = contact.account
        public_id = contact.public_id
        contact.delete()
        if hs_object_id:
            from apps.hubspot.sync import enqueue_contact_delete

            enqueue_contact_delete(account, hs_object_id)

    from apps.core.audit import platform_record

    platform_record(
        action="contact.delete",
        account=account,
        actor=actor,
        resource_type="contact",
        resource_id=public_id,
        metadata={"source": source, "whatsapp": len(wa), "instagram": len(ig)},
    )
    return {"contacts": 1, "whatsapp": len(wa), "instagram": len(ig)}


def _hubspot_link(contact):
    Link = apps.get_model("hubspot", "HubSpotContactLink")
    return Link.objects.filter(contact=contact).first()

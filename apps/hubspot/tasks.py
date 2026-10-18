"""HubSpot Celery tasks."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    bind=True, max_retries=3, default_retry_delay=120, acks_late=True, queue="celery"
)
def push_new_contact_to_hubspot(self, contact_id: int) -> None:
    """Create the HubSpot contact for a new Akilent contact, if the account is connected.

    Runs off the request/webhook path: a new contact is usually created while an inbound
    WhatsApp or Instagram message is being processed, and a slow or failing HubSpot call
    must not hold that up. HubSpot API errors are logged by ``create_hs_contact_from_local``;
    anything unexpected is retried, and an existing link makes a retry a no-op.
    """
    from apps.contacts.models import Contact
    from apps.hubspot.create import create_hs_contact_from_local
    from apps.hubspot.models import HubSpotConnection

    contact = Contact.objects.filter(pk=contact_id).select_related("account").first()
    if contact is None:
        return
    conn = HubSpotConnection.objects.filter(
        account=contact.account, status=HubSpotConnection.Status.ACTIVE
    ).first()
    if conn is None:
        return
    try:
        link = create_hs_contact_from_local(contact, conn)
    except Exception as exc:
        logger.warning("hubspot: push of contact %s failed: %s", contact_id, exc)
        raise self.retry(exc=exc) from exc
    if link:
        logger.info(
            "hubspot: created HS contact %s for Akilent contact %s",
            link.hs_object_id,
            contact_id,
        )

"""HubSpot Django signal handlers."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def on_contact_saved(sender, instance, created, **kwargs):
    """When a new Akilent Contact is saved, queue its push to HubSpot if connected.

    Only fires on creation (not updates — updates go through the sync job queue
    via enqueue_contact_sync, triggered by the views that change contact data).
    The HubSpot call itself runs in ``tasks.push_new_contact_to_hubspot`` after the
    transaction commits; this handler only checks for a connection, so it never
    slows down or breaks the contact save.
    """
    if not created:
        return

    # Contact was imported from HubSpot — don't push it back.
    if getattr(instance, "source", None) == "hubspot":
        return

    try:
        from django.db import transaction

        from apps.hubspot.models import HubSpotConnection
        from apps.hubspot.tasks import push_new_contact_to_hubspot

        if not HubSpotConnection.objects.filter(
            account_id=instance.account_id,
            status=HubSpotConnection.Status.ACTIVE,
        ).exists():
            return
        contact_id = instance.pk
        transaction.on_commit(lambda: push_new_contact_to_hubspot.delay(contact_id))
    except Exception as exc:
        logger.warning(
            "hubspot: on_contact_saved failed for contact %s: %s", instance.pk, exc
        )

"""HubSpot Django signal handlers."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def on_contact_saved(sender, instance, created, **kwargs):
    """When a new Akilent Contact is saved, push it to HubSpot if connected.

    Only fires on creation (not updates — updates go through the sync job queue
    via enqueue_contact_sync, triggered by the views that change contact data).
    Runs synchronously; failures are logged and swallowed so they never break
    the contact save.
    """
    if not created:
        return

    # Contact was imported from HubSpot — don't push it back.
    if getattr(instance, "source", None) == "hubspot":
        return

    # Avoid import-time circular imports by importing inside the handler.
    try:
        from apps.hubspot.create import create_hs_contact_from_local
        from apps.hubspot.models import HubSpotConnection

        try:
            conn = HubSpotConnection.objects.get(
                account=instance.account,
                status=HubSpotConnection.Status.ACTIVE,
            )
        except HubSpotConnection.DoesNotExist:
            return

        link = create_hs_contact_from_local(instance, conn)
        if link:
            logger.info(
                "hubspot: created HS contact %s for Akilent contact %s",
                link.hs_object_id,
                instance.pk,
            )
    except Exception as exc:
        logger.warning(
            "hubspot: on_contact_saved failed for contact %s: %s", instance.pk, exc
        )

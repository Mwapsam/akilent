"""HubSpot ↔ Akilent contact creation for bidirectional sync.

create_hs_contact_from_local(contact, connection)
    Push a new Akilent contact to HubSpot and save the resulting link.

create_local_contact_from_hs(account, hs_obj)
    Create an Akilent Contact from a HubSpot contact object and link it.
"""

from __future__ import annotations

import logging

from django.db import transaction

from apps.hubspot.field_map import CONTACT_FIELD_MAP
from apps.hubspot.models import HubSpotContactLink

logger = logging.getLogger(__name__)


def create_hs_contact_from_local(contact, connection) -> HubSpotContactLink | None:
    """Create a new HubSpot contact from an Akilent Contact and link them.

    Returns the new HubSpotContactLink, or None if creation failed.
    Already-linked contacts are skipped silently.
    """
    if HubSpotContactLink.objects.filter(contact=contact).exists():
        return None

    from apps.hubspot.client import HubSpotAPIError, get_fresh_client

    try:
        client = get_fresh_client(connection)
    except HubSpotAPIError as exc:
        logger.warning("create_hs_contact: token refresh failed: %s", exc)
        return None

    properties: dict[str, str] = {}
    for local_field, hs_field in CONTACT_FIELD_MAP.items():
        value = getattr(contact, local_field, None)
        if value:
            properties[hs_field] = str(value)

    if not properties.get("email"):
        # HubSpot requires email to create a contact.
        logger.info("create_hs_contact: skipping contact %s — no email", contact.pk)
        return None

    try:
        data = client.post("/crm/v3/objects/contacts", json={"properties": properties})
    except HubSpotAPIError as exc:
        if exc.status_code == 409:
            # Conflict: a contact with this email already exists in HubSpot.
            # Fetch it and link instead of duplicating.
            return _link_existing_hs_contact_by_email(
                client, contact, connection.account, properties["email"]
            )
        logger.warning(
            "create_hs_contact: API error for contact %s: %s", contact.pk, exc
        )
        return None

    hs_id = str(data.get("id", ""))
    if not hs_id:
        return None

    with transaction.atomic():
        link, created = HubSpotContactLink.objects.get_or_create(
            account=connection.account,
            hs_object_id=hs_id,
            defaults={"contact": contact},
        )
        if not created:
            # Another process raced us — link already exists.
            logger.info("create_hs_contact: link already exists for hs_id %s", hs_id)
    return link


def create_local_contact_from_hs(account, hs_obj: dict) -> HubSpotContactLink | None:
    """Create an Akilent Contact from a HubSpot object dict and link them.

    Returns the new HubSpotContactLink, or None on error / duplicate.
    """
    from apps.contacts.models import Contact

    hs_id = str(hs_obj.get("id", ""))
    props = hs_obj.get("properties", {})
    email = (props.get("email") or "").strip().lower() or None

    if not hs_id:
        return None

    # Already linked — nothing to do.
    if HubSpotContactLink.objects.filter(account=account, hs_object_id=hs_id).exists():
        return None

    # If a local contact with this email already exists, link it instead of
    # creating a duplicate.
    if email:
        existing = Contact.objects.filter(account=account, email__iexact=email).first()
        if existing:
            with transaction.atomic():
                link, _ = HubSpotContactLink.objects.get_or_create(
                    account=account,
                    hs_object_id=hs_id,
                    defaults={"contact": existing},
                )
            return link

    first_name = (props.get("firstname") or "").strip()
    last_name = (props.get("lastname") or "").strip()
    phone = (props.get("phone") or "").strip() or None

    if not email and not phone:
        logger.info(
            "create_local_contact: hs %s has no email or phone — skipped", hs_id
        )
        return None

    try:
        with transaction.atomic():
            contact = Contact.objects.create(
                account=account,
                email=email,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                source="hubspot",
            )
            # The post_save signal may have already created the link (it fires
            # synchronously inside Contact.objects.create). Use get_or_create so
            # we don't race against ourselves.
            link, _ = HubSpotContactLink.objects.get_or_create(
                account=account,
                contact=contact,
                defaults={"hs_object_id": hs_id},
            )
    except Exception as exc:
        logger.warning("create_local_contact: failed for hs %s: %s", hs_id, exc)
        return None

    return link


def _link_existing_hs_contact_by_email(
    client, contact, account, email: str
) -> HubSpotContactLink | None:
    """Fetch an existing HubSpot contact by email and link it to a local contact."""
    from apps.hubspot.client import HubSpotAPIError

    try:
        data = client.post(
            "/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "email",
                                "operator": "EQ",
                                "value": email,
                            }
                        ]
                    }
                ],
                "properties": ["email"],
                "limit": 1,
            },
        )
    except HubSpotAPIError as exc:
        logger.warning("_link_existing_hs_contact: search failed: %s", exc)
        return None

    results = data.get("results", [])
    if not results:
        return None

    hs_id = str(results[0].get("id", ""))
    if not hs_id:
        return None

    with transaction.atomic():
        link, _ = HubSpotContactLink.objects.get_or_create(
            account=account,
            hs_object_id=hs_id,
            defaults={"contact": contact},
        )
    return link

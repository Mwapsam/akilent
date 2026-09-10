"""Contact upsert, CSV import, and activity recording."""
from __future__ import annotations

import csv
import io
import logging

from django.utils import timezone

from apps.contacts.models import Contact, ContactEvent, ContactImport

logger = logging.getLogger(__name__)

_ENGAGEMENT_EVENTS = {"opened", "clicked"}


def upsert_contact(account, email: str, *, attributes: dict | None = None, **fields) -> tuple[Contact, bool]:
    """Create or update a contact by (account, email). Returns (contact, created)."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")

    contact, created = Contact.objects.get_or_create(
        account=account, email=email, defaults=fields
    )
    dirty = []
    for key, value in fields.items():
        if value not in (None, "") and getattr(contact, key, None) != value:
            setattr(contact, key, value)
            dirty.append(key)
    if attributes:
        merged = {**(contact.attributes or {}), **attributes}
        if merged != contact.attributes:
            contact.attributes = merged
            dirty.append("attributes")
    if dirty and not created:
        contact.save(update_fields=[*set(dirty), "updated_at"])
    elif created and (attributes or dirty):
        contact.save()

    trigger = "contact.created" if created else "contact.updated"
    _notify_contact(contact, trigger)
    _trigger_workflows(contact, trigger)
    return contact, created


def _trigger_workflows(contact, trigger_type: str, context: dict | None = None) -> None:
    try:
        from apps.automation.workflow_engine import enroll_for_trigger

        enroll_for_trigger(contact.account_id, trigger_type, contact, context=context)
    except Exception:
        logger.exception("_trigger_workflows failed (%s) for %s", trigger_type, contact.pk)


def _notify_contact(contact, event_type: str) -> None:
    try:
        from apps.email.webhooks import notify

        notify(contact.account, event_type, {
            "id": contact.public_id,
            "email": contact.email,
            "status": contact.status,
        })
    except Exception:
        logger.exception("_notify_contact failed for %s", contact.pk)


def record_contact_event(contact: Contact, event_type: str, *, occurred_at=None, data: dict | None = None) -> ContactEvent:
    ev = ContactEvent.objects.create(
        contact=contact,
        account_id=contact.account_id,
        type=event_type,
        occurred_at=occurred_at or timezone.now(),
        data=data or {},
    )
    kind = event_type.split(".")[-1]  # "email.opened" -> "opened"
    updates = []
    if kind in _ENGAGEMENT_EVENTS:
        contact.last_engaged_at = ev.occurred_at
        updates.append("last_engaged_at")
        _trigger_workflows(contact, f"email.{kind}", {"event": data or {}})
    if kind == "unsubscribed" and contact.status != Contact.Status.UNSUBSCRIBED:
        contact.status = Contact.Status.UNSUBSCRIBED
        updates.append("status")
    elif kind == "bounced" and contact.status == Contact.Status.SUBSCRIBED:
        contact.status = Contact.Status.BOUNCED
        updates.append("status")
    elif kind == "complained":
        contact.status = Contact.Status.COMPLAINED
        updates.append("status")
    if updates:
        contact.save(update_fields=[*updates, "updated_at"])
        if "status" in updates and kind == "unsubscribed":
            _notify_contact(contact, "contact.unsubscribed")
    return ev


def import_csv(account, text: str, *, filename: str = "", mapping: dict | None = None) -> ContactImport:
    """Import contacts from CSV text.

    ``mapping`` maps CSV header -> contact field or ``attr:<key>``. Unmapped
    columns are ignored. A column mapped to ``email`` (or a header literally
    named ``email``) is required.
    """
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    mapping = mapping or {h: h for h in headers}
    email_col = next((src for src, dst in mapping.items() if dst == "email"), None)
    if email_col is None and "email" in headers:
        email_col = "email"
    if email_col is None:
        raise ValueError("CSV import needs a column mapped to 'email'")

    imp = ContactImport.objects.create(account=account, filename=filename, mapping=mapping)
    created = updated = skipped = rows = 0

    for row in reader:
        rows += 1
        email = (row.get(email_col) or "").strip()
        if not email:
            skipped += 1
            continue
        fields: dict = {"source": "import"}
        attributes: dict = {}
        for src, dst in mapping.items():
            val = (row.get(src) or "").strip()
            if not val or dst == "email":
                continue
            if dst.startswith("attr:"):
                attributes[dst[5:]] = val
            elif dst in {"first_name", "last_name", "locale"}:
                fields[dst] = val
        try:
            _, was_created = upsert_contact(account, email, attributes=attributes, **fields)
            created += was_created
            updated += (not was_created)
        except Exception:
            logger.exception("import_csv: row %s failed", rows)
            skipped += 1

    imp.row_count = rows
    imp.created_count = created
    imp.updated_count = updated
    imp.skipped_count = skipped
    imp.save(update_fields=["row_count", "created_count", "updated_count", "skipped_count"])
    return imp

"""Data requests an operator carries out for a business: export, delete one customer, close.

These back the promises on /data-deletion/. Deletion is permanent, so the console asks the
operator to type a confirmation first; each function only touches the given business's rows.
"""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

CLOSE_AFTER = timedelta(days=30)
EXPORT_FIELDS = ["first_name", "last_name", "email", "phone", "status", "consent_status",
                 "tags", "first_seen", "last_engaged_at"]


def contacts_csv(account) -> str:
    from apps.contacts.models import Contact

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(EXPORT_FIELDS)
    for c in Contact.objects.filter(account=account).prefetch_related("tags").order_by("first_seen").iterator(chunk_size=500):
        writer.writerow([
            c.first_name, c.last_name, c.email or "", c.phone or "", c.status, c.consent_status,
            ";".join(t.name for t in c.tags.all()),
            c.first_seen.isoformat() if c.first_seen else "",
            c.last_engaged_at.isoformat() if c.last_engaged_at else "",
        ])
    return out.getvalue()


def find_customer(account, who: str):
    """``(contacts, whatsapp_contacts)`` matching a phone number or email in this business."""
    from django.core.exceptions import ValidationError

    from apps.contacts.models import Contact
    from apps.whatsapp.models import WhatsAppContact
    from apps.whatsapp.models.contact import normalize_phone

    who = (who or "").strip()
    if not who:
        return [], []
    if "@" in who:
        contacts = list(Contact.objects.filter(account=account, email__iexact=who))
        wa = list(WhatsAppContact.objects.filter(account=account, contact__in=contacts))
        return contacts, wa
    try:
        phone = normalize_phone(who)
    except ValidationError:
        return [], []
    wa = list(WhatsAppContact.objects.filter(account=account, phone_number=phone))
    contacts = list(Contact.objects.filter(account=account, phone=phone)) + [
        w.contact for w in wa if w.contact_id]
    return list({c.pk: c for c in contacts}.values()), wa


def delete_customer(account, who: str) -> dict:
    """Delete one customer's contact record, conversations, messages and related history.

    Contact deletion cascades to spine conversations and messages, orders, leads, deals and
    automation runs; the WhatsApp contact (and its message log and send queue) is deleted too.
    """
    contacts, wa = find_customer(account, who)
    if not contacts and not wa:
        return {"contacts": 0, "whatsapp": 0}
    with transaction.atomic():
        for w in wa:
            w.delete()
        for c in contacts:
            c.delete()
    return {"contacts": len(contacts), "whatsapp": len(wa)}


def close_account(account) -> None:
    """Suspend now; everything is deleted after 30 days unless it's reopened."""
    account.is_active = False
    account.scheduled_deletion_at = timezone.now() + CLOSE_AFTER
    account.save(update_fields=["is_active", "scheduled_deletion_at"])


def delete_due_accounts(now=None) -> int:
    """Delete closed businesses whose 30 days have passed (a daily beat task)."""
    from apps.accounts.models import Account

    now = now or timezone.now()
    due = Account.objects.filter(is_active=False, scheduled_deletion_at__lte=now)
    n = 0
    for account in due:
        account.delete()
        n += 1
    return n

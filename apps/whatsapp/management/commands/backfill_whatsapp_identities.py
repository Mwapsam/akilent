"""Give existing customers with a phone number a WhatsApp identity.

Customers added by hand, imported or synced before identities were created for
every customer have no WhatsAppContact, so campaigns skipped them as "no
WhatsApp number". New customers get one automatically (apps.whatsapp.signals);
this catches up the ones already stored. Safe to re-run; never changes opt-in.

    python manage.py backfill_whatsapp_identities [--account <id>] [--dry-run]
"""

from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from apps.contacts import api as contacts_api
from apps.whatsapp.models import WhatsAppContact
from apps.whatsapp.services.contacts import ensure_whatsapp_identity


class Command(BaseCommand):
    help = "Create WhatsApp identities for customers who have a phone but none yet."

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, help="Only this account id.")
        parser.add_argument(
            "--dry-run", action="store_true", help="Count only; change nothing."
        )

    def handle(self, *args, **options):
        contacts = (
            contacts_api.contacts_with_phone(options.get("account"))
            .annotate(
                has_identity=Exists(
                    WhatsAppContact.objects.filter(contact=OuterRef("pk"))
                )
            )
            .filter(has_identity=False)
        )

        if options["dry_run"]:
            self.stdout.write(
                f"{contacts.count()} customers would get a WhatsApp identity."
            )
            return

        linked = unusable = 0
        for contact in contacts.iterator(chunk_size=500):
            identity = ensure_whatsapp_identity(contact)
            if identity is not None and identity.contact_id == contact.pk:
                linked += 1
            else:
                unusable += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Linked {linked} customers. Skipped {unusable} "
                "(invalid number, or the number belongs to another customer)."
            )
        )

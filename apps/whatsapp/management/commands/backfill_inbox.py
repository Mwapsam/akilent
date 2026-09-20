"""Project already-received WhatsApp messages into the Inbox.

Before the Inbox stopped depending on the automation-events flag, inbound
messages were logged (MessageLog) but never appeared in the Inbox. This
recreates those conversations. Safe to re-run; never starts Workflows.

    python manage.py backfill_inbox [--account <id>]
"""
from django.core.management.base import BaseCommand

from apps.whatsapp.models import MessageLog
from apps.whatsapp.tasks import project_to_inbox


class Command(BaseCommand):
    help = "Create Inbox conversations for inbound WhatsApp messages that lack one."

    def add_arguments(self, parser):
        parser.add_argument("--account", type=int, help="Only this account id.")

    def handle(self, *args, **options):
        from apps.conversations.models import Message

        logs = MessageLog.objects.filter(direction=MessageLog.Direction.INBOUND).select_related(
            "account", "contact", "conversation"
        ).order_by("timestamp")
        if options.get("account"):
            logs = logs.filter(account_id=options["account"])

        done = skipped = 0
        contacts = self._link_contacts(options.get("account"))
        for log in logs:
            if Message.objects.filter(whatsapp_message=log).exists():
                skipped += 1
                continue
            project_to_inbox(log.account, log.contact, log.conversation, log, enroll_workflows=False)
            done += 1
        self.stdout.write(self.style.SUCCESS(
            f"Backfilled {done} message(s); {skipped} already in the Inbox. "
            f"Linked {contacts} WhatsApp contact(s) to Contacts."
        ))

    def _link_contacts(self, account_id) -> int:
        """Every WhatsApp identity should have a canonical Contact (phone-only is fine)."""
        from apps.contacts.services import upsert_contact_by_phone
        from apps.whatsapp.models import WhatsAppContact

        qs = WhatsAppContact.objects.filter(contact__isnull=True).select_related("account")
        if account_id:
            qs = qs.filter(account_id=account_id)
        linked = 0
        for wa in qs:
            try:
                contact, created = upsert_contact_by_phone(wa.account, wa.phone_number, source="whatsapp")
            except Exception as exc:
                self.stderr.write(f"Skipped {wa.phone_number}: {exc}")
                continue
            if created and wa.display_name and not contact.first_name:
                contact.first_name = wa.display_name[:150]
                contact.save(update_fields=["first_name", "updated_at"])
            wa.contact = contact
            wa.save(update_fields=["contact"])
            linked += 1
        return linked

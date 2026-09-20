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
        for log in logs:
            if Message.objects.filter(whatsapp_message=log).exists():
                skipped += 1
                continue
            project_to_inbox(log.account, log.contact, log.conversation, log, enroll_workflows=False)
            done += 1
        self.stdout.write(self.style.SUCCESS(f"Backfilled {done} message(s); {skipped} already in the Inbox."))

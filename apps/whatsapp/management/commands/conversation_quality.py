"""Read-only check that the conversation spine is trustworthy (Phase 1 instrumentation).

    poetry run python manage.py conversation_quality (--account <id|slug> | --all-accounts) [--json]

Reports, per account and independently: conversations whose state is indeterminate,
conversations with invalid message ordering, and conversations with missing outbound
(a reply the provider log says was sent that never reached the inbox). Counts only.
"""
import json

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from apps.conversations.quality import spine_quality
from apps.whatsapp.models import MessageLog

_SENT = (MessageLog.Status.SENT, MessageLog.Status.DELIVERED, MessageLog.Status.READ)


def missing_outbound(account) -> int:
    """Conversations holding a sent provider reply that has no spine message."""
    return (
        MessageLog.objects.filter(
            account_id=account.id, direction=MessageLog.Direction.OUTBOUND,
            status__in=_SENT, generic_message__isnull=True,
        )
        .values("conversation_id").distinct().count()
    )


class Command(BaseCommand):
    help = "Read-only trustworthiness counters for the conversation spine."

    def add_arguments(self, parser):
        who = parser.add_mutually_exclusive_group(required=True)
        who.add_argument("--account", help="Account id or slug.")
        who.add_argument("--all-accounts", action="store_true")
        parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    def handle(self, *args, **options):
        Account = apps.get_model("accounts", "Account")  # no cross-app model import
        if options["all_accounts"]:
            accounts = list(Account.objects.order_by("id"))
        else:
            ref = options["account"]
            accounts = list(Account.objects.filter(id=int(ref) if ref.isdigit() else None)
                            or Account.objects.filter(slug=ref))
            if not accounts:
                raise CommandError(f"No account matches {ref!r}")

        entries = []
        for account in accounts:
            counts = spine_quality(account)
            counts["conversations_with_missing_outbound"] = missing_outbound(account)
            entries.append({"account": {"id": account.id, "slug": account.slug}, **counts})

        if options["json"]:
            self.stdout.write(json.dumps({"accounts": entries}, indent=2, sort_keys=True))
            return
        for e in entries:
            self.stdout.write(f"Account {e['account']['slug']} (id {e['account']['id']})")
            for key in ("conversations_with_indeterminate_state",
                        "conversations_with_missing_outbound",
                        "conversations_with_invalid_ordering"):
                self.stdout.write(f"  {key}: {e[key]}")

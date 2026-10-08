"""Archive inbox conversations created from another Instagram account's webhooks.

When two professional accounts that both authorized Akilent message each other,
Meta sends a webhook for each side. Before webhooks were matched strictly, the
other side's copies were filed under the connected business, creating
conversations whose "customer" is the business itself.

    python manage.py instagram_archive_misfiled            # list only
    python manage.py instagram_archive_misfiled --apply    # archive them

A conversation is misfiled when every webhook message in it names a business
other than the connected account. Archiving closes it as spam and marks its open
leads lost; nothing is deleted, so it can be reopened from the Closed tab.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.conversations import api as conversations_api
from apps.instagram.models import InstagramConversation
from apps.instagram.models.message import InstagramMessage


def business_party(metadata: dict) -> str:
    """The business side of a stored webhook message: echo sender, else recipient."""
    if (metadata.get("message") or {}).get("is_echo"):
        return (metadata.get("sender") or {}).get("id", "")
    return (metadata.get("recipient") or {}).get("id", "")


def is_misfiled(ig_convo: InstagramConversation) -> bool:
    own = ig_convo.instagram_account.webhook_ids
    parties = {
        business_party(m.metadata or {})
        for m in InstagramMessage.objects.filter(conversation=ig_convo)
    }
    parties.discard("")  # sent through Akilent: no webhook copy to judge by
    return bool(parties) and not (parties & own)


class Command(BaseCommand):
    help = "Archive Instagram conversations created from another account's webhooks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Archive them (default: list only)."
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        found = 0
        for ig_convo in InstagramConversation.objects.select_related(
            "instagram_account", "instagram_contact"
        ).order_by("pk"):
            if not is_misfiled(ig_convo):
                continue
            found += 1
            conversation = conversations_api.conversation_for_channel_record(
                "instagram", ig_convo.pk
            )
            label = (
                conversation.public_id if conversation else "(no inbox conversation)"
            )
            line = (
                f"{label} account=@{ig_convo.instagram_account.username} "
                f"customer_igsid={ig_convo.instagram_contact.instagram_scoped_id}"
            )
            if apply:
                leads = 0
                if conversation is not None:
                    leads = conversations_api.archive_as_misfiled(
                        conversation, actor="system:instagram-cleanup"
                    )
                if ig_convo.is_open:
                    ig_convo.is_open = False
                    ig_convo.save(update_fields=["is_open"])
                line += f" -> archived, {leads} lead(s) marked lost"
            self.stdout.write(line)

        verb = "Archived" if apply else "Found"
        self.stdout.write(f"{verb} {found} misfiled conversation(s).")
        if found and not apply:
            self.stdout.write("Run again with --apply to archive them.")

"""Link existing Instagram comment threads to the Content Studio posts they're on.

Comments that arrived before post attribution existed have no ``content_variant``.
This fills it in, and records the post as the entry point of any DM a private reply
opened, so leads and orders from those chats carry the post as their source.

    python manage.py instagram_link_comment_posts [--dry-run]

Safe to run more than once. Leads and orders already created keep their attribution:
it is a historical record and is never rewritten.
"""

from django.core.management.base import BaseCommand

from apps.conversations.entry_points import record_comment_entry
from apps.instagram.models import CommentThread
from apps.instagram.services.inbound import link_thread_to_content


class Command(BaseCommand):
    help = "Link Instagram comment threads to the Content Studio posts they belong to."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        linked = entries = 0
        threads = CommentThread.objects.filter(content_variant__isnull=True).exclude(
            post_id=""
        )
        for thread in threads.select_related("instagram_account").iterator():
            if dry:
                from apps.publishing.models import ContentPostVariant

                if ContentPostVariant.objects.filter(
                    platform="instagram",
                    publication_id=thread.post_id,
                    post__account_id=thread.instagram_account.account_id,
                ).exists():
                    linked += 1
                continue
            link_thread_to_content(thread)
            if thread.content_variant_id:
                linked += 1
                if record_comment_entry(thread):
                    entries += 1
        verb = "Would link" if dry else "Linked"
        self.stdout.write(
            f"{verb} {linked} comment thread(s) to Content Studio posts; "
            f"{entries} DM conversation(s) now carry the post as their entry point."
        )

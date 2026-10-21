"""List (and optionally merge) customers whose emails differ only by case.

Run before migration contacts 0010, which makes emails unique per business
regardless of case:

    python manage.py check_contact_emails            # report only
    python manage.py check_contact_emails --merge    # merge each group

The oldest customer in each group is kept; the others are merged into it
(apps.contacts.merge.merge_contacts — everything they had moves across).
"""

from django.core.management.base import BaseCommand
from django.db.models import Count
from django.db.models.functions import Lower

from apps.contacts.merge import MergeError, merge_contacts
from apps.contacts.models import Contact


class Command(BaseCommand):
    help = "Find customers whose emails differ only by upper/lower case; --merge them."

    def add_arguments(self, parser):
        parser.add_argument("--merge", action="store_true")

    def handle(self, *args, **opts):
        groups = (
            Contact.objects.filter(email__isnull=False)
            .annotate(e=Lower("email"))
            .values("account_id", "e")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
        )
        found = merged = failed = 0
        for g in groups:
            found += 1
            people = list(
                Contact.objects.filter(
                    account_id=g["account_id"], email__iexact=g["e"]
                ).order_by("first_seen", "pk")
            )
            self.stdout.write(
                f"account {g['account_id']}: {g['e']} -> "
                + ", ".join(p.public_id for p in people)
            )
            if not opts["merge"]:
                continue
            keep = people[0]
            for dup in people[1:]:
                try:
                    merge_contacts(keep, dup)
                    merged += 1
                except MergeError as exc:
                    failed += 1
                    self.stderr.write(f"  could not merge {dup.public_id}: {exc}")
        self.stdout.write(
            self.style.SUCCESS(
                f"{found} group(s) found"
                + (f", {merged} merged, {failed} failed" if opts["merge"] else "")
            )
        )

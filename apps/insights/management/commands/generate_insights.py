"""Management command: generate business insights for one or all accounts.

Usage
-----
    poetry run python manage.py generate_insights
    poetry run python manage.py generate_insights --account <slug>
"""

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run all insight rules and upsert results for one or all accounts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            metavar="SLUG",
            help="Only run for the account with this slug.",
        )

    def handle(self, *args, **options):
        from apps.accounts.models import Account
        from apps.insights.engine import run_all_rules

        slug = options.get("account")
        if slug:
            qs = Account.objects.filter(slug=slug, is_active=True)
            if not qs.exists():
                self.stderr.write(
                    self.style.ERROR(f"No active account with slug '{slug}'.")
                )
                return
        else:
            qs = Account.objects.filter(is_active=True)

        total = qs.count()
        self.stdout.write(f"Running insight rules for {total} account(s)...")

        grand = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}

        for account in qs.iterator():
            summary = run_all_rules(account)
            for k in grand:
                grand[k] += summary.get(k, 0)
            if options.get("verbosity", 1) >= 2:
                self.stdout.write(
                    f"  {account.slug}: "
                    f"created={summary['created']} updated={summary['updated']} "
                    f"skipped={summary['skipped']} errors={summary['errors']}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. created={grand['created']} updated={grand['updated']} "
                f"skipped={grand['skipped']} errors={grand['errors']}"
            )
        )

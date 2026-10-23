from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.billing import credit
from apps.billing.money import format_money


class Command(BaseCommand):
    help = (
        "Give every pilot business early-adopter account credit (once per business). "
        "Credit is used before any card is charged."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "amount", help="Credit per business, in BILLING_CURRENCY, e.g. 150"
        )
        parser.add_argument("--wave", type=int, help="Only this pilot wave")
        parser.add_argument(
            "--days", type=int, help="Expire unused credit after N days"
        )
        parser.add_argument(
            "--note", default="", help="Shown to operators, e.g. 'Wave 1'"
        )
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, amount, wave, days, note, dry_run, **options):
        expires = timezone.now() + timedelta(days=days) if days else None
        try:
            granted, skipped = credit.grant_early_adopters(
                amount, wave=wave, note=note, expires_at=expires, dry_run=dry_run
            )
        except credit.CreditError as exc:
            raise CommandError(str(exc)) from exc
        verb = "Would give" if dry_run else "Gave"
        for account in granted:
            self.stdout.write(
                f"{verb} {format_money(amount)} to {account.company_name}"
            )
        for account in skipped:
            self.stdout.write(
                f"Skipped {account.company_name} (already has it, or inactive)"
            )
        self.stdout.write(
            self.style.SUCCESS(f"{len(granted)} granted, {len(skipped)} skipped")
        )

"""Management command: enroll an account as a pilot (Phase 5).

Usage
-----
    poetry run python manage.py enroll_pilot --account <slug>
    poetry run python manage.py enroll_pilot --account <slug> --wave 2
    poetry run python manage.py enroll_pilot --account <slug> --dry-run

Exit codes
----------
    0 — enrolled successfully
    1 — account not found, setup score below gate, or other error
"""

import sys

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Enroll an account as a pilot and snapshot baseline metrics."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            metavar="SLUG",
            required=True,
            help="Account slug to enroll.",
        )
        parser.add_argument(
            "--wave",
            type=int,
            default=2,
            metavar="N",
            help="Pilot wave number (default: 2).",
        )
        parser.add_argument(
            "--enrolled-by",
            metavar="EMAIL",
            help="Email of the staff user recording this enrollment.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Print the setup score without saving the enrollment.",
        )

    def handle(self, *args, **options):
        from apps.accounts.models import Account
        from apps.accounts.pilot import _SETUP_SCORE_MINIMUM, enroll_pilot, setup_score

        slug = options["account"]
        wave = options["wave"]
        dry_run = options["dry_run"]

        try:
            account = Account.objects.get(slug=slug, is_active=True)
        except Account.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"No active account with slug {slug!r}"))
            sys.exit(1)

        score = setup_score(account)
        self.stdout.write(f"Setup score for {account.slug!r}: {score}/100")

        if dry_run:
            if score >= _SETUP_SCORE_MINIMUM:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  ✓ Score ≥ {_SETUP_SCORE_MINIMUM} — ready for pilot enrollment."
                    )
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"  ✗ Score < {_SETUP_SCORE_MINIMUM} — not yet ready."
                    )
                )
            return

        enrolled_by = None
        if options.get("enrolled_by"):
            try:
                enrolled_by = User.objects.get(email=options["enrolled_by"])
            except User.DoesNotExist:
                self.stderr.write(
                    self.style.WARNING(
                        f"User {options['enrolled_by']!r} not found — enrolling without enrolled_by."
                    )
                )

        try:
            enrollment = enroll_pilot(account, wave=wave, enrolled_by=enrolled_by)
        except ValueError as exc:
            self.stderr.write(self.style.ERROR(str(exc)))
            sys.exit(1)

        self.stdout.write(
            self.style.SUCCESS(
                f"Enrolled {account.slug!r} in wave {enrollment.wave} "
                f"at {enrollment.enrolled_at.strftime('%Y-%m-%d %H:%M UTC')}.\n"
                f"  Baseline: avg response "
                f"{enrollment.baseline_avg_response_seconds:.0f}s"
                if enrollment.baseline_avg_response_seconds
                else f"Enrolled {account.slug!r} in wave {enrollment.wave} "
                f"at {enrollment.enrolled_at.strftime('%Y-%m-%d %H:%M UTC')}.\n"
                f"  Baseline: avg response N/A"
            )
        )
        self.stdout.write(
            f"  Leads: {enrollment.baseline_lead_count}, "
            f"Conversations: {enrollment.baseline_conversation_count}"
        )

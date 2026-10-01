"""Management command: print pilot outcome report (Phase 5).

Usage
-----
    poetry run python manage.py pilot_report
    poetry run python manage.py pilot_report --account <slug>
    poetry run python manage.py pilot_report --wave 2

Exit codes
----------
    0 — always (this is a read-only report command)
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print pilot outcome metrics (current vs enrollment baseline)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--account",
            metavar="SLUG",
            help="Restrict report to one account slug.",
        )
        parser.add_argument(
            "--wave",
            type=int,
            metavar="N",
            help="Restrict report to one wave number.",
        )

    def handle(self, *args, **options):
        from apps.accounts.models import PilotEnrollment
        from apps.accounts.pilot import pilot_outcome

        qs = PilotEnrollment.objects.select_related("account", "enrolled_by").order_by(
            "wave", "enrolled_at"
        )

        if options.get("account"):
            qs = qs.filter(account__slug=options["account"])
        if options.get("wave"):
            qs = qs.filter(wave=options["wave"])

        if not qs.exists():
            self.stdout.write("No pilot enrollments found.")
            return

        for enrollment in qs:
            account = enrollment.account
            outcome = pilot_outcome(account)
            self._print_pilot(enrollment, outcome)

    def _print_pilot(self, enrollment, outcome):
        sep = "─" * 60
        self.stdout.write(f"\n{sep}")
        self.stdout.write(
            f"Wave {enrollment.wave}  |  {enrollment.account.slug}"
            + (
                f"  |  enrolled by {enrollment.enrolled_by.email}"
                if enrollment.enrolled_by
                else ""
            )
        )
        self.stdout.write(
            f"Enrolled: {enrollment.enrolled_at.strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self.stdout.write(f"Setup score (current): {outcome['setup_score']}/100")

        # Response time
        baseline_rt = outcome.get("baseline_avg_response_seconds")
        current_rt = outcome.get("current_avg_response_seconds")
        if baseline_rt is not None and current_rt is not None:
            improvement = outcome.get("response_time_improvement_pct", 0)
            direction = "↓" if improvement > 0 else ("↑" if improvement < 0 else "→")
            self.stdout.write(
                f"Avg response time:  {baseline_rt:.0f}s → {current_rt:.0f}s  "
                f"({direction} {abs(improvement):.1f}%)"
            )
        elif current_rt is not None:
            self.stdout.write(f"Avg response time:  {current_rt:.0f}s  (no baseline)")
        else:
            self.stdout.write("Avg response time:  N/A")

        # Lead growth
        baseline_leads = outcome.get("baseline_lead_count", 0)
        current_leads = outcome.get("current_lead_count", 0)
        leads_added = outcome.get("leads_added", 0)
        self.stdout.write(
            f"Leads:              {baseline_leads} → {current_leads}  "
            f"(+{leads_added} added)"
        )

        # Conversation growth
        baseline_conv = outcome.get("baseline_conversation_count")
        current_conv = outcome.get("current_conversation_count")
        if baseline_conv is not None and current_conv is not None:
            self.stdout.write(
                f"Conversations:      {baseline_conv} → {current_conv}  "
                f"(+{current_conv - baseline_conv} added)"
            )

        if enrollment.notes:
            self.stdout.write(f"Notes: {enrollment.notes}")

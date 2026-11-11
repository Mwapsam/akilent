from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "List billing dates that were missed and payments stuck between states "
        "(the same check the daily billing_health_check task runs). --alert also "
        "emails superusers and posts to Slack."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--alert",
            action="store_true",
            help="Email superusers and post to Slack when something is found.",
        )

    def handle(self, *args, **options):
        from apps.billing import health

        found = health.problems()
        if not found:
            self.stdout.write(self.style.SUCCESS("Billing check: nothing to report."))
            return
        self.stdout.write(self.style.WARNING(f"Billing check: {len(found)} problem(s)"))
        for line in found:
            self.stdout.write(f"- {line}")
        if options["alert"]:
            health.alert(found)

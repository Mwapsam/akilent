"""``manage.py ai_ping``: prove the configured AI provider (and its fallback) answers. Sends no customer data."""

from django.core.management.base import BaseCommand, CommandError

from apps.ai import api as ai_api
from apps.ai.providers import (
    backend_name,
    fallback_configured,
    fallback_name,
    is_configured,
)


class Command(BaseCommand):
    help = (
        "Send one tiny request to the configured AI provider, and to the fallback when one is "
        "configured, and report the model and latency of each."
    )

    def handle(self, *args, **options):
        if not is_configured():
            raise CommandError(
                "AI is not configured. Set AI_PROVIDER_BACKEND (for example 'ollama')."
            )
        checks = [("primary", backend_name())]
        if fallback_configured():
            checks.append(("fallback", f"fallback {fallback_name()}"))
        failed = []
        for which, label in checks:
            result = ai_api.test_connection(which)
            if result["ok"]:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{label} OK: model {result['model']} answered in {result['latency_ms']} ms."
                    )
                )
            else:
                self.stderr.write(self.style.ERROR(f"{label}: {result['error']}"))
                failed.append(label)
        if len(failed) == len(checks):
            raise CommandError("No AI provider answered.")
        if failed:
            self.stdout.write(
                self.style.WARNING(
                    "AI still works through the other provider, but fix: "
                    + ", ".join(failed)
                )
            )

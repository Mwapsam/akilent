"""``manage.py ai_ping``: prove the configured AI provider answers. Sends no customer data."""
from django.core.management.base import BaseCommand, CommandError

from apps.ai import api as ai_api
from apps.ai.providers import backend_name, is_configured


class Command(BaseCommand):
    help = "Send one tiny request to the configured AI provider and report the model and latency."

    def handle(self, *args, **options):
        if not is_configured():
            raise CommandError("AI is not configured. Set AI_PROVIDER_BACKEND (for example 'ollama').")
        result = ai_api.test_connection()
        if not result["ok"]:
            raise CommandError(f"{backend_name()}: {result['error']}")
        self.stdout.write(self.style.SUCCESS(
            f"{backend_name()} OK: model {result['model']} answered in {result['latency_ms']} ms."))

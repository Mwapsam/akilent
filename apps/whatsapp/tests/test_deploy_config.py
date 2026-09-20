"""Deploy config guard: a queue that tasks are routed to must have a consumer.

WhatsApp inbound events are routed to the ``whatsapp`` queue. When the compose
worker didn't consume it, webhooks were accepted (200) but never processed, so
no message was ever logged and setup could never complete.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

COMPOSE = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def worker_queues() -> set[str]:
    text = COMPOSE.read_text(encoding="utf-8")
    block = text[text.index("celery_worker:"):]
    match = re.search(r"celery -A automator worker[^\n]*?-Q\s+(\S+)", block)
    assert match, "celery_worker command has no -Q list"
    return set(match.group(1).split(","))


class WorkerQueuesTest(SimpleTestCase):
    def test_worker_consumes_the_whatsapp_queue(self):
        self.assertIn("whatsapp", worker_queues())

    def test_worker_consumes_the_outbound_queue(self):
        self.assertIn("outbound", worker_queues())

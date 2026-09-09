"""Sandbox send provider — deterministic outcomes, nothing delivered.

Selected automatically for messages created with a ``test`` API key
(``ak_test_…``). The recipient's local-part chooses the simulated outcome so
integrations can exercise every lifecycle branch end to end:

    delivered@…   -> queued -> sent -> delivered   (default for anything else)
    bounce@…      -> queued -> sent -> bounced
    complaint@…   -> queued -> sent -> delivered -> complained
    open@…        -> … -> delivered -> opened
    click@…       -> … -> delivered -> opened -> clicked
    fail@…        -> queued -> failed (no send)

The follow-up events are emitted on a short Celery countdown so timelines and
outbound webhooks behave just like production.
"""
from __future__ import annotations

import logging

from apps.email.providers.send_base import EmailSendProvider
from apps.email.types import OutboundEmail, SendResult

logger = logging.getLogger(__name__)

_OUTCOME_EVENTS = {
    "bounce": ["sent", "bounced"],
    "complaint": ["sent", "delivered", "complained"],
    "open": ["sent", "delivered", "opened"],
    "click": ["sent", "delivered", "opened", "clicked"],
    "fail": ["failed"],
    "delivered": ["sent", "delivered"],
}


def outcome_for(to_email: str) -> str:
    local = (to_email or "").split("@", 1)[0].lower()
    local = local.split("+", 1)[0]
    return local if local in _OUTCOME_EVENTS else "delivered"


def followup_events(to_email: str) -> list[str]:
    """Events after the synchronous `sent`/`failed` the pipeline already emits."""
    events = _OUTCOME_EVENTS[outcome_for(to_email)]
    return events[1:]  # drop the leading sent/failed


class SandboxSendProvider(EmailSendProvider):
    def send(self, message: OutboundEmail) -> SendResult:
        outcome = outcome_for(message.to_email)
        logger.info("SandboxSendProvider: %s -> %s (not delivered)", message.to_email, outcome)
        if outcome == "fail":
            return SendResult(success=False, error="sandbox: simulated permanent failure")
        return SendResult(success=True, provider_message_id=f"sandbox-{id(message):x}")

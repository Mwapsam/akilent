"""Reconciliation hook consumed by ``apps.facebook`` on permanent send failure.

Same as ``apps.automation.integrations.instagram``, for Messenger outbox rows.
"""


def mark_outbound_message_failed(message) -> None:
    """Reconcile a permanently-failed ``FacebookOutboundMessage`` onto its step/run."""
    from apps.automation.integrations.instagram import (
        mark_outbound_message_failed as reconcile,
    )

    reconcile(message, fk="fb_outbound_message_id", label="Messenger")

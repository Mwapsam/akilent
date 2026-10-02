"""Support services package.

Public helper used by all service modules:
    record_event(ticket, event_type, actor=None, **metadata)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def record_event(ticket, event_type: str, actor=None, **metadata):
    """Create an immutable SupportEvent row for a ticket state change."""
    from apps.support.models import SupportEvent

    try:
        return SupportEvent.objects.create(
            ticket=ticket,
            event_type=event_type,
            actor=actor,
            metadata=metadata,
        )
    except Exception:
        logger.exception(
            "record_event failed: ticket=%s event_type=%s", ticket.pk, event_type
        )
        return None

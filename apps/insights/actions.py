"""Insight action service — closing the detection → action → outcome loop.

execute_recommendation() links an accepted recommendation to the conversation it
produces, recording provenance so the full chain is queryable:

    Insight → RecommendationLog → Conversation → ConversationAttribution → Lead/Order

record_outcome_signal() updates the progressive outcome signals on a
RecommendationLog as downstream business events occur:

    dm_sent → dm_replied → lead_created → order_created → order_paid

This module is the bridge between the intelligence layer (insights) and the
action layer (conversations, DMs, leads, orders).  It never creates business
objects — callers do that — but it wires provenance and records measurements.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from apps.insights.models import Insight

logger = logging.getLogger(__name__)

# All recognised progressive outcome signals in causal order.
OUTCOME_SIGNAL_ORDER = [
    "dm_sent_at",
    "dm_replied_at",
    "lead_created_at",
    "order_created_at",
    "order_paid_at",
]


def execute_recommendation(
    rec_log,
    *,
    conversation,
    action_type: str,
) -> None:
    """Record that a recommendation was acted on and link the resulting conversation.

    Sets rec_log.status = ACCEPTED, rec_log.conversation, rec_log.action_type,
    rec_log.acted_at, and propagates the provenance onto ConversationAttribution
    rows that exist for this conversation so the backward chain is queryable.

    Idempotent: calling twice with the same conversation is a no-op.
    """
    from apps.conversations.models import ConversationAttribution
    from apps.insights.models import RecommendationLog

    if rec_log.conversation_id is not None:
        return  # already executed

    rec_log.status = RecommendationLog.Status.ACCEPTED
    rec_log.accepted = True
    rec_log.action_type = action_type
    rec_log.conversation = conversation
    rec_log.acted_at = timezone.now()
    rec_log.save(
        update_fields=["status", "accepted", "action_type", "conversation", "acted_at"]
    )

    # Back-fill provenance on any attribution rows that already exist for this
    # conversation (rare at execute time, but possible on replay).
    ConversationAttribution.objects.filter(
        conversation=conversation,
        originated_from__isnull=True,
    ).update(originated_from=rec_log)


def record_outcome_signal(
    rec_log,
    signal: str,
    *,
    revenue: str | None = None,
    currency: str | None = None,
) -> None:
    """Record a single progressive outcome signal on a RecommendationLog.

    ``signal`` must be one of OUTCOME_SIGNAL_ORDER.  Signals are timestamped
    once and never overwritten — re-recording the same signal is a no-op.
    ``outcome_measured_at`` is set to now() on the first signal recorded.
    """
    if signal not in OUTCOME_SIGNAL_ORDER:
        raise ValueError(
            f"Unknown outcome signal: {signal!r}. Must be one of {OUTCOME_SIGNAL_ORDER}"
        )

    signals = dict(rec_log.outcome_summary)
    if signals.get(signal):
        return  # already recorded — idempotent

    signals[signal] = timezone.now().isoformat()
    if revenue is not None:
        signals["revenue"] = revenue
    if currency is not None:
        signals["currency"] = currency

    update_fields = ["outcome_summary"]
    rec_log.outcome_summary = signals

    if not rec_log.outcome_measured_at:
        rec_log.outcome_measured_at = timezone.now()
        update_fields.append("outcome_measured_at")

    rec_log.save(update_fields=update_fields)


def attach_attribution_provenance(attribution, rec_log) -> None:
    """Wire an existing ConversationAttribution back to its originating recommendation.

    Called when a Lead or Order is attributed to a conversation that was produced
    by a recommendation — completing the backward chain:
        Order.attribution.originated_from → RecommendationLog → Insight

    Uses QuerySet.update() to bypass ConversationAttribution's immutability guard,
    which only blocks .save() to prevent accidental history mutation — provenance
    wiring is intentional and post-creation.
    """
    if attribution.originated_from_id is not None:
        return
    type(attribution).objects.filter(pk=attribution.pk).update(originated_from=rec_log)
    attribution.originated_from = rec_log
    attribution.originated_from_id = rec_log.pk


def insight_for_order(order) -> Insight | None:
    """Return the Insight that originated the conversation leading to this order, if any.

    Traverses: Order → ConversationAttribution → RecommendationLog → Insight
    Returns None when the order has no attribution, no provenance, or no linked insight.
    """
    try:
        attribution = order.attribution
    except Exception:
        return None
    if attribution.originated_from_id is None:
        return None
    return attribution.originated_from.insight

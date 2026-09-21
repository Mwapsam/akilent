"""Is the conversation spine trustworthy? Read-only, channel-neutral counters.

These are engineering measurements (not customer analytics) used by the "re-measure"
step of Phase 1 to check that who-spoke-last can be relied on:

* ``conversations_with_indeterminate_state`` - the data can't say who spoke last (no
  message, or only unsent outbound).
* ``conversations_with_invalid_ordering`` - the message recorded most recently is stamped
  earlier than another message by more than ``ORDERING_TOLERANCE``. Who-spoke-last is
  derived from timestamps, so a clock/ordering anomaly can flip it. A few seconds or
  minutes of webhook delay is normal and is not counted.

A third counter, ``conversations_with_missing_outbound``, needs provider knowledge and is
computed by the channel adapter (see ``apps.whatsapp``'s ``conversation_quality`` command).
"""
from __future__ import annotations

from datetime import timedelta
from itertools import groupby

from django.utils import timezone

from apps.conversations.models import Conversation, Message
from apps.conversations.state import ConversationState, get_conversation_state

ORDERING_TOLERANCE = timedelta(minutes=5)


def spine_quality(account, now=None) -> dict:
    now = now or timezone.now()

    indeterminate = sum(
        get_conversation_state(c, now).state is ConversationState.INDETERMINATE
        for c in Conversation.objects.filter(account=account).iterator()
    )

    invalid = 0
    rows = (
        Message.objects.filter(account=account)
        .order_by("conversation_id", "created_at", "id")
        .values_list("conversation_id", "timestamp")
    )
    for _cid, group in groupby(rows.iterator(), key=lambda r: r[0]):
        stamps = [ts for _c, ts in group]           # oldest-recorded first
        if max(stamps) - stamps[-1] > ORDERING_TOLERANCE:   # newest-recorded isn't the latest-stamped
            invalid += 1

    return {
        "conversations_with_indeterminate_state": indeterminate,
        "conversations_with_invalid_ordering": invalid,
    }

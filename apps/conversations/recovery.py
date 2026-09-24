"""Missed-conversation recovery: turn "customer never got a reply" into a team reminder.

``state.missed`` already *shows* open WhatsApp conversations the customer wrote to that
went 24h+ unanswered. This is the step that acts on them: it gives each one a due
:class:`FollowUp`, so it appears in the dashboard's "follow-ups due" and the follow-ups page
without anyone having to spot it. It only ever creates an internal reminder - nothing is
sent to the customer, so WhatsApp's consent and 24h-window rules are not involved.

Idempotent: a conversation is reminded once per customer message. It is skipped while a
follow-up created since the customer's latest message exists (open or done), and reminded
again only if the customer writes again and is missed again.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.db.models import Max
from django.utils import timezone

from apps.conversations.models import Conversation, FollowUp
from apps.conversations.state import INACTIVITY_WINDOW, _unanswered, with_activity

# Conversations older than this are left alone so the first run never floods a team with
# months of history; recovery is about recent enquiries.
DEFAULT_MAX_AGE = timedelta(days=7)
NOTE = "No reply sent - customer has been waiting"


def create_missed_followups(now: datetime | None = None, *, max_age: timedelta = DEFAULT_MAX_AGE) -> int:
    """Create one due follow-up per missed WhatsApp conversation. Returns how many."""
    now = now or timezone.now()
    missed = list(
        with_activity(
            Conversation.objects.filter(
                channel=Conversation.Channel.WHATSAPP, status=Conversation.Status.OPEN
            )
        )
        .filter(
            _unanswered(),
            last_in__lte=now - INACTIVITY_WINDOW,
            last_in__gte=now - max_age,
        )
    )
    latest_reminder = dict(
        FollowUp.objects.filter(conversation__in=[c.pk for c in missed])
        .values_list("conversation_id")
        .annotate(latest=Max("created_at"))
    )
    followups = [
        FollowUp(account_id=c.account_id, contact_id=c.contact_id, conversation=c, due_at=now, note=NOTE)
        for c in missed
        if latest_reminder.get(c.pk) is None or latest_reminder[c.pk] < c.last_in
    ]
    FollowUp.objects.bulk_create(followups)
    return len(followups)

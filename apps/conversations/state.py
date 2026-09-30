"""Derived conversation state (Phase 1).

Central invariant: for every active conversation Akilent can tell who spoke last,
whether the customer is waiting on the business, how long, and what happened most
recently. Nothing here is stored - it is all derived from ``Message`` rows and the
conversation's own ``status``, and this module is the one place the rules live. The
inbox, metrics and tests all consume it; the channel adapters only feed messages in.

The rules (locked in the plan):

* **Business responded** = an outbound message that was actually sent (status in
  ``RESPONSE_STATUSES``). Queued and failed outbound messages are *not* a response,
  so a failed reply leaves the customer waiting rather than hiding them.
* ``last_customer_message_at`` = the latest inbound message. ``inactive_24h`` =
  ``now - last_customer_message_at >= 24h``. **Only the customer's messages drive
  this clock: an outbound message (human, template or workflow) never resets it**,
  so an agent template does not make an otherwise dead conversation look alive.
  It is evaluated on demand and never stored.
* ``CLOSED`` = explicit close (which includes STOP) OR ``inactive_24h``. A later
  customer message re-opens the conversation (``Conversation.register_inbound``), so
  it becomes eligible for attention again.
* ``needs_attention`` (= ``WAITING_FOR_AGENT``) = not ``CLOSED`` AND the customer
  spoke last. Waiting age = ``now - last_customer_message_at``. There is no stored
  flag; an SLA threshold may later change sorting or urgency but never membership.
* "Spoke last" compares timestamps strictly: on an exact tie the business is
  treated as having responded.

A consequence worth knowing: because ``inactive_24h`` counts as closed, a customer who
was never answered drops out of needs-attention after 24h. ``missed`` surfaces exactly
those (still-open conversations the customer wrote to, unanswered for 24h+). ``missed`` is
a derived recovery queue - the state semantics above are not bent to fit the UI.

Closing and re-opening (kept deliberately different):

* **Explicit close** - the *same* conversation is closed; a later customer message
  re-opens it (``Conversation.register_inbound``) and it is eligible for attention again.
* **STOP** - terminal for the exchange. The channel adapter closes the conversation, and a
  later customer message starts a *new* conversation (WhatsApp opens a fresh
  ``whatsapp.Conversation``), leaving the old one closed.

Both paths are pinned by ``apps/whatsapp/tests/test_phase1_acceptance.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.db.models import F, Max, Min, Q
from django.utils import timezone

from apps.conversations.models import Conversation, Message

INACTIVITY_WINDOW = timedelta(hours=24)
RESPONSE_STATUSES = ("sent", "delivered", "read")

# A waiting customer crosses from "urgent" (amber) to "overdue" (red) here — a
# tighter, actionable warning well before INACTIVITY_WINDOW's 24h auto-close/
# "missed" ceiling. Fixed for now, not a per-account setting: see the plan doc
# before adding one, so it doesn't get reinvented per feature that wants a
# response-time threshold.
OVERDUE_WAITING = timedelta(hours=2)


class ConversationState(StrEnum):
    WAITING_FOR_AGENT = "waiting_for_agent"
    WAITING_FOR_CUSTOMER = "waiting_for_customer"
    CLOSED = "closed"
    INDETERMINATE = (
        "indeterminate"  # the data can't tell us (no message from either side)
    )


@dataclass(frozen=True)
class ConversationSnapshot:
    state: ConversationState
    last_speaker: str | None  # "customer" | "business" | None
    last_customer_message_at: datetime | None
    last_business_message_at: datetime | None
    last_activity_at: datetime | None  # any message, including queued/failed outbound
    waiting_age: timedelta | None  # only when WAITING_FOR_AGENT
    closed_reason: str | None  # "closed" | "inactive_24h" | None
    missed: bool  # open, customer spoke last, unanswered for 24h+
    first_response_seconds: float | None

    @property
    def needs_attention(self) -> bool:
        return self.state is ConversationState.WAITING_FOR_AGENT

    @property
    def overdue(self) -> bool:
        """Waiting long enough to escalate past the plain "urgent" styling.

        Deliberately a stricter subset of ``needs_attention`` — every overdue
        conversation needs attention, but most waiting conversations are still
        within a normal response window. ``missed`` conversations (24h+) are
        always overdue too; this just fires earlier.
        """
        return self.waiting_age is not None and self.waiting_age >= OVERDUE_WAITING


def _customer_spoke_last(last_in, last_out) -> bool:
    return last_in is not None and (last_out is None or last_in > last_out)


def derive_state(
    *, status, last_in, last_out, last_any, now, first_response_seconds=None
):
    """Pure derivation from the four facts every caller can obtain in one query."""
    inactive = last_in is not None and now - last_in >= INACTIVITY_WINDOW
    closed = status == Conversation.Status.CLOSED
    customer_last = _customer_spoke_last(last_in, last_out)

    if last_any is None or (last_in is None and last_out is None):
        state, waiting = ConversationState.INDETERMINATE, None
    elif closed or inactive:
        state, waiting = ConversationState.CLOSED, None
    elif customer_last:
        state, waiting = ConversationState.WAITING_FOR_AGENT, now - last_in
    else:
        state, waiting = ConversationState.WAITING_FOR_CUSTOMER, None

    if last_in is None and last_out is None:
        speaker = None
    else:
        speaker = "customer" if customer_last else "business"

    closed_reason = None
    if state is ConversationState.CLOSED:
        closed_reason = "closed" if closed else "inactive_24h"

    return ConversationSnapshot(
        state=state,
        last_speaker=speaker,
        last_customer_message_at=last_in,
        last_business_message_at=last_out,
        last_activity_at=last_any,
        waiting_age=waiting,
        closed_reason=closed_reason,
        missed=(not closed) and inactive and customer_last,
        first_response_seconds=first_response_seconds,
    )


def _response_filter():
    return Q(direction=Message.Direction.OUTBOUND, status__in=RESPONSE_STATUSES)


def calculate_response_time(conversation: Conversation) -> float | None:
    """Seconds from the customer's first message to the business's first actual reply.

    ``None`` when there is no customer message or no reply yet (never invented).
    """
    facts = conversation.messages.aggregate(
        first_in=Min("timestamp", filter=Q(direction=Message.Direction.INBOUND)),
    )
    first_in = facts["first_in"]
    if first_in is None:
        return None
    reply = conversation.messages.filter(
        _response_filter(), timestamp__gte=first_in
    ).aggregate(first_reply=Min("timestamp"))["first_reply"]
    return None if reply is None else (reply - first_in).total_seconds()


def get_conversation_state(
    conversation: Conversation, now: datetime | None = None
) -> ConversationSnapshot:
    now = now or timezone.now()
    facts = conversation.messages.aggregate(
        last_in=Max("timestamp", filter=Q(direction=Message.Direction.INBOUND)),
        last_out=Max("timestamp", filter=_response_filter()),
        last_any=Max("timestamp"),
    )
    return derive_state(
        status=conversation.status,
        now=now,
        first_response_seconds=calculate_response_time(conversation),
        **facts,
    )


def with_activity(qs):
    """Annotate conversations with the facts ``derive_state`` needs (one query)."""
    return qs.annotate(
        last_in=Max(
            "messages__timestamp",
            filter=Q(messages__direction=Message.Direction.INBOUND),
        ),
        last_out=Max(
            "messages__timestamp",
            filter=Q(
                messages__direction=Message.Direction.OUTBOUND,
                messages__status__in=RESPONSE_STATUSES,
            ),
        ),
        last_any=Max("messages__timestamp"),
    )


def snapshot_of(
    annotated: Conversation, now: datetime | None = None
) -> ConversationSnapshot:
    """Snapshot for a conversation produced by :func:`with_activity`."""
    return derive_state(
        status=annotated.status,
        last_in=annotated.last_in,  # type: ignore[attr-defined]
        last_out=annotated.last_out,  # type: ignore[attr-defined]
        last_any=annotated.last_any,  # type: ignore[attr-defined]
        now=now or timezone.now(),
    )


def unanswered_q():
    """The customer spoke last, on the facts :func:`with_activity` annotates.

    Public (not module-private) because callers outside this module — the
    inbox's "overdue" filter toggle — need the identical definition rather
    than a hand-rolled copy, per this module's own "one place the rules
    live" rule.
    """
    return Q(last_in__isnull=False) & (
        Q(last_out__isnull=True) | Q(last_in__gt=F("last_out"))
    )


def needs_attention(account, now: datetime | None = None):
    """Conversations waiting on the business, longest-waiting first. A pure derived query."""
    now = now or timezone.now()
    return (
        with_activity(
            Conversation.objects.filter(
                account=account, status=Conversation.Status.OPEN
            )
        )
        .filter(unanswered_q(), last_in__gt=now - INACTIVITY_WINDOW)
        .order_by("last_in")
    )


def with_first_response(qs):
    """Annotate ``first_in`` and ``first_reply`` (the facts behind ``calculate_response_time``) in
    the same query, so a list of conversations never costs a query each."""
    from django.db.models import OuterRef, Subquery

    first_in = (
        Message.objects.filter(
            conversation=OuterRef("pk"), direction=Message.Direction.INBOUND
        )
        .order_by("timestamp", "id")
        .values("timestamp")[:1]
    )
    reply = (
        Message.objects.filter(
            _response_filter(),
            conversation=OuterRef("pk"),
            timestamp__gte=OuterRef("first_in"),
        )
        .order_by("timestamp", "id")
        .values("timestamp")[:1]
    )
    return qs.annotate(first_in=Subquery(first_in)).annotate(
        first_reply=Subquery(reply)
    )


def response_seconds_of(annotated) -> float | None:
    """``calculate_response_time`` for a conversation from :func:`with_first_response`."""
    if annotated.first_in is None or annotated.first_reply is None:  # type: ignore[attr-defined]
        return None
    return (annotated.first_reply - annotated.first_in).total_seconds()  # type: ignore[attr-defined]


def average_first_response_seconds(account, *, limit: int = 100) -> float | None:
    """Average first-response time (seconds) over the most recently active
    answered conversations. Deliberately not a stored/cached metric: the same
    definition as ``calculate_response_time`` (the acceptance test and the
    composer status line rely on it), computed for all of them in one query.
    ``None`` when there's no determinate sample yet (never invented).
    """
    candidates = with_first_response(
        with_activity(Conversation.objects.filter(account=account))
        .filter(last_in__isnull=False, last_out__isnull=False)
        .order_by("-last_any")
    )[:limit]
    samples = [s for c in candidates if (s := response_seconds_of(c)) is not None]
    if not samples:
        return None
    return sum(samples) / len(samples)


def missed(account, now: datetime | None = None):
    """Open conversations the customer wrote to that went 24h+ with no business reply."""
    now = now or timezone.now()
    return (
        with_activity(
            Conversation.objects.filter(
                account=account, status=Conversation.Status.OPEN
            )
        )
        .filter(unanswered_q(), last_in__lte=now - INACTIVITY_WINDOW)
        .order_by("last_in")
    )

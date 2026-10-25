"""Follow-up reminders for the team: when they're due, and the one way they're created.

A follow-up is a note to *your team* to get back to a customer. It never messages the
customer. Every path that makes one (a person, an AI suggestion, missed-conversation
recovery, an Insights rule, an automation step) goes through ``create`` so there is one
duplicate guard and one set of rules about who it belongs to.

Times are the business's own: "tomorrow morning" is 09:00 where the business is
(``BusinessHours.timezone``), and a picked time is read in that zone too. Everything is
stored in UTC.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

_TOMORROW_HOUR = 9

# The choices offered when setting or snoozing a reminder.
CHOICES = (
    ("1h", "In 1 hour"),
    ("3h", "In 3 hours"),
    ("tomorrow", "Tomorrow morning"),
    ("next_week", "Next week"),
    ("custom", "Pick a time"),
)


def business_zone(account) -> ZoneInfo:
    from apps.accounts import business_hours

    try:
        return ZoneInfo(business_hours.workspace_timezone(account) or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def resolve_due_at(
    choice: str, custom_value: str = "", *, account=None
) -> datetime.datetime:
    """The UTC instant a "remind me …" choice means for ``account``'s timezone."""
    now = timezone.now()
    zone = business_zone(account) if account is not None else ZoneInfo("UTC")
    local_now = now.astimezone(zone)
    if choice == "1h":
        return now + datetime.timedelta(hours=1)
    if choice == "3h":
        return now + datetime.timedelta(hours=3)
    if choice in ("tomorrow", "next_week"):
        days = 1 if choice == "tomorrow" else 7
        local = (local_now + datetime.timedelta(days=days)).replace(
            hour=_TOMORROW_HOUR, minute=0, second=0, microsecond=0
        )
        return local.astimezone(datetime.UTC)
    if choice == "custom":
        parsed = parse_datetime(custom_value) if custom_value else None
        if parsed is None:
            raise ValueError("Pick a valid date and time.")
        if timezone.is_naive(parsed):
            parsed = parsed.replace(tzinfo=zone)
        parsed = parsed.astimezone(datetime.UTC)
        if parsed <= now:
            raise ValueError("Pick a time in the future.")
        return parsed
    raise ValueError("Choose when to be reminded.")


def local_label(dt: datetime.datetime, account) -> str:
    """``"Tue 4 Nov, 09:00 (CAT)"``: a due time as the business reads it."""
    local = dt.astimezone(business_zone(account))
    return f"{local:%a} {local.day} {local:%b}, {local:%H:%M} ({local.tzname()})"


def create(
    account,
    contact,
    *,
    due_at: datetime.datetime,
    conversation=None,
    note: str = "",
    source: str = "manual",
    created_by=None,
    assigned_to=None,
):
    """Make a reminder, or return the open one of the same kind on this conversation.

    Returns ``(followup, created)``. An existing open reminder is kept as it is (a second
    click, a repeated AI suggestion or the next sweep must not stack copies), except that
    an earlier due time wins, so asking again "sooner" works.
    """
    from apps.conversations.models import FollowUp

    if assigned_to is None:
        if created_by is not None:
            assigned_to = created_by
        elif conversation is not None:
            assigned_to = getattr(conversation, "assigned_to", None)
    # One transaction for the check and the insert: the row lock below needs it (Postgres
    # refuses FOR UPDATE outside one), and the savepoint lets a lost race fall back cleanly.
    with transaction.atomic():
        if conversation is not None:
            existing = (
                FollowUp.objects.select_for_update()
                .filter(conversation=conversation, source=source, done_at__isnull=True)
                .first()
            )
            if existing is not None:
                if due_at < existing.due_at:
                    existing.due_at = due_at
                    existing.notified_at = None
                    existing.save(update_fields=["due_at", "notified_at"])
                return existing, False
        try:
            with transaction.atomic():
                followup = FollowUp.objects.create(
                    account=account,
                    contact=contact,
                    conversation=conversation,
                    due_at=due_at,
                    note=(note or "")[:255],
                    source=source,
                    created_by=created_by,
                    assigned_to=assigned_to,
                )
        except IntegrityError:
            # Someone else made the same one a moment ago.
            followup = FollowUp.objects.get(
                conversation=conversation, source=source, done_at__isnull=True
            )
            return followup, False
    _announce(followup)
    return followup, True


def snooze(followup, choice: str, custom_value: str = "") -> None:
    followup.due_at = resolve_due_at(choice, custom_value, account=followup.account)
    followup.notified_at = None
    followup.snoozed_count += 1
    followup.save(update_fields=["due_at", "notified_at", "snoozed_count"])
    _announce(followup)


def reassign(followup, user) -> None:
    followup.assigned_to = user
    followup.save(update_fields=["assigned_to"])
    _announce(followup)


def edit(followup, *, note: str | None = None, due_at=None) -> None:
    fields = []
    if note is not None:
        followup.note = note[:255]
        fields.append("note")
    if due_at is not None:
        followup.due_at = due_at
        followup.notified_at = None
        fields += ["due_at", "notified_at"]
    if fields:
        followup.save(update_fields=fields)
        _announce(followup)


def close(followup, *, by=None, reason: str = "done") -> None:
    followup.mark_done(by=by, reason=reason)
    _announce(followup)


def reopen(followup) -> None:
    """Undo a close (an accidental "Done"). Refused if another open one of its kind exists."""
    from apps.conversations.models import FollowUp

    if followup.done_at is None:
        return
    if (
        followup.conversation_id
        and FollowUp.objects.filter(
            conversation_id=followup.conversation_id,
            source=followup.source,
            done_at__isnull=True,
        ).exists()
    ):
        raise ValueError("There's already an open reminder like this one.")
    followup.done_at = None
    followup.closed_reason = ""
    followup.done_by = None
    followup.notified_at = None
    followup.save(update_fields=["done_at", "closed_reason", "done_by", "notified_at"])
    _announce(followup)


# Reminders that only mean "the customer is waiting on us": a reply from the team ends them.
WAITING_SOURCES = ("missed", "policy")


def close_when_team_replies(conversation) -> int:
    """The team answered: reminders that only existed because nobody had are done."""
    from apps.conversations.models import FollowUp

    closed = 0
    for followup in FollowUp.objects.filter(
        conversation=conversation, source__in=WAITING_SOURCES, done_at__isnull=True
    ):
        close(followup, reason=FollowUp.ClosedReason.STAFF_REPLIED)
        closed += 1
    return closed


def open_after_customer_replied(conversation):
    """Reminders a person set on this conversation that are still open after the customer
    wrote again. The pane asks "still need this?" instead of guessing."""
    from apps.conversations.models import FollowUp, Message

    last_inbound = (
        Message.objects.filter(
            conversation=conversation, direction=Message.Direction.INBOUND
        )
        .order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    if last_inbound is None:
        return []
    qs = FollowUp.objects.filter(
        conversation=conversation, done_at__isnull=True
    ).exclude(source__in=WAITING_SOURCES)
    return list(qs.filter(created_at__lt=last_inbound))


def _announce(followup) -> None:
    from apps.core import realtime

    realtime.publish(
        followup.account_id,
        "followup.updated",
        {
            "followup_id": followup.pk,
            "conversation_id": getattr(followup.conversation, "public_id", "")
            if followup.conversation_id
            else "",
        },
    )


# ── "It's due" ───────────────────────────────────────────────────────────────────────────

_NOTIFY_BATCH = 500
_EMAIL_EVERY_SECONDS = 3600


def notify_due(now=None) -> int:
    """Nudge the team about reminders that have just fallen due. Returns how many.

    Each reminder nudges once per due time (snoozing re-arms it): a live toast and badge for
    everyone in the business, and, for the person whose list it's on, at most one email an
    hour listing what's due. "Missed conversation" reminders get no email here: the overdue
    alert already emails whoever is looking after that conversation.
    """
    from django.core.cache import cache

    from apps.accounts.models import Membership
    from apps.conversations.models import FollowUp
    from apps.core import realtime

    now = now or timezone.now()
    with transaction.atomic():
        # Lock only the reminder rows: FOR UPDATE can't reach the nullable side of the
        # conversation/assignee joins on Postgres, and shouldn't hold accounts or contacts.
        ids = list(
            FollowUp.objects.select_for_update(
                skip_locked=_can_skip_locked(), of=("self",)
            )
            .filter(
                done_at__isnull=True,
                notified_at__isnull=True,
                due_at__lte=now,
                account__is_active=True,
            )
            .order_by("due_at")
            .values_list("pk", flat=True)[:_NOTIFY_BATCH]
        )
        FollowUp.objects.filter(pk__in=ids).update(notified_at=now)
    due = list(
        FollowUp.objects.filter(pk__in=ids)
        .select_related("account", "contact", "conversation", "assigned_to")
        .order_by("due_at")
    )

    by_person: dict[tuple[int, int], list] = {}
    for f in due:
        realtime.publish(
            f.account_id,
            "followup.due",
            {
                "followup_id": f.pk,
                "conversation_id": f.conversation.public_id if f.conversation else "",
            },
        )
        if f.source != FollowUp.Source.MISSED and f.assigned_to_id:
            by_person.setdefault((f.account_id, f.assigned_to_id), []).append(f)

    for (account_id, user_id), items in by_person.items():
        wants = Membership.objects.filter(
            account_id=account_id, user_id=user_id, followup_emails=True
        ).exists()
        if not wants or not cache.add(
            f"followup-due-email:{account_id}:{user_id}", 1, _EMAIL_EVERY_SECONDS
        ):
            continue
        _email_due(items)
    return len(due)


def _email_due(items) -> None:
    from django.urls import reverse

    from apps.accounts import notifications

    first = items[0]
    lines = [
        f"• {f.contact.display_name}: {f.note or 'follow up'} "
        f"(due {local_label(f.due_at, f.account)})"
        for f in items[:10]
    ]
    if len(items) > 10:
        lines.append(f"…and {len(items) - 10} more.")
    try:
        notifications.notify_team(
            first.account,
            to=first.assigned_to.email,
            subject=(
                f"Akilent: {len(items)} follow-up{'s' if len(items) != 1 else ''} due"
            ),
            text="These reminders on your follow-up list are due now:\n\n"
            + "\n".join(lines),
            path=reverse("conversations:followups_due") + "?list=mine",
            reason="You're getting this because these reminders are on your follow-up "
            "list. Turn these emails off on the Follow-ups page.",
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "could not email due follow-ups to user %s", first.assigned_to_id
        )


def _can_skip_locked() -> bool:
    from django.db import connection

    return connection.features.has_select_for_update_skip_locked

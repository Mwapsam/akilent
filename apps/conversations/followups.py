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
# What Later offers wherever a follow-up is moved (the page and the side panel), beside
# "Pick a date".
LATER_CHOICES = tuple(c for c in CHOICES if c[0] in ("1h", "tomorrow", "next_week"))


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
    from apps.conversations.followup_copy import CHOOSE_WHEN

    raise ValueError(CHOOSE_WHEN)


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
    for_team: bool = False,
):
    """Make a reminder, or return the open one of the same kind on this conversation.

    With no ``assigned_to`` it goes to whoever made it, else the chat's assignee; with
    ``for_team`` it stays for the whole team (an automation step set to "Whole team").

    Returns ``(followup, created)``. An existing open reminder is kept (a second click, a
    repeated AI suggestion or the next sweep must not stack copies), except that an earlier
    due time wins, so asking again "sooner" works, and a new note is added to its note
    rather than dropped (``append_note``). ``followup.note_result`` says what became of it.
    """
    from apps.conversations.models import FollowUp

    if assigned_to is None and not for_team:
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
                fields = []
                if due_at < existing.due_at:
                    existing.due_at = due_at
                    existing.notified_at = None
                    fields += ["due_at", "notified_at"]
                note_result = append_note(existing, note)
                existing.note_result = note_result  # type: ignore[attr-defined]
                if note_result == NOTE_ADDED:
                    fields.append("note")
                if fields:
                    existing.save(update_fields=fields)
                    _announce(existing)
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
            note_result = append_note(followup, note)
            followup.note_result = note_result  # type: ignore[attr-defined]
            if note_result == NOTE_ADDED:
                followup.save(update_fields=["note"])
            return followup, False
    _announce(followup)
    return followup, True


NOTE_MAX = 255
NOTE_ADDED, NOTE_ALREADY_THERE, NOTE_TOO_LONG = "added", "already_there", "too_long"


def append_note(followup, note: str) -> str:
    """Add ``note`` to ``followup.note`` on a line of its own (unsaved), never replacing or
    cutting what's there. Returns ``""`` for a blank note (nothing to add),
    ``NOTE_ALREADY_THERE`` when that exact line is already in it (an automation repeating
    itself), ``NOTE_TOO_LONG`` when the two won't fit in the note (left as it was, so the
    person can be told), else ``NOTE_ADDED``."""
    note = (note or "").strip()
    if not note:
        return ""
    current = followup.note or ""
    if note in (line.strip() for line in current.splitlines()):
        return NOTE_ALREADY_THERE
    combined = f"{current}\n{note}" if current else note
    if len(combined) > NOTE_MAX:
        return NOTE_TOO_LONG
    followup.note = combined
    return NOTE_ADDED


class FollowUpClosed(ValueError):
    """The follow-up was closed (by someone else, a moment ago) before this change landed."""

    def __init__(self):
        from apps.conversations.followup_copy import ALREADY_DONE

        super().__init__(ALREADY_DONE)


def _write_open(followup, fields: dict) -> None:
    """Apply ``fields`` to the row only while it is still open, under a row lock.

    Two people acting at once (Done and Later, say) must leave one consistent state: the
    second change sees the row closed and is refused instead of resurrecting it. The caller's
    instance is updated to what was saved.
    """
    from apps.conversations.models import FollowUp

    with transaction.atomic():
        locked = FollowUp.objects.select_for_update().get(pk=followup.pk)
        if locked.done_at is not None:
            followup.done_at = locked.done_at
            followup.closed_reason = locked.closed_reason
            raise FollowUpClosed()
        for name, value in fields.items():
            setattr(locked, name, value)
        locked.save(update_fields=list(fields))
    for name, value in fields.items():
        setattr(followup, name, value)


def snooze(followup, choice: str, custom_value: str = "") -> None:
    """Later: move it to a new time. Refused (``FollowUpClosed``) once it's closed."""
    due_at = resolve_due_at(choice, custom_value, account=followup.account)
    _write_open(
        followup,
        {
            "due_at": due_at,
            "notified_at": None,
            "snoozed_count": followup.snoozed_count + 1,
        },
    )
    _announce(followup)


def reassign(followup, user) -> None:
    _write_open(followup, {"assigned_to": user})
    _announce(followup)


def edit(followup, *, note: str | None = None, due_at=None) -> None:
    fields: dict = {}
    if note is not None:
        # A textarea sends "\r\n"; stored as "\n" so a line break costs one character.
        fields["note"] = note.replace("\r\n", "\n")[:NOTE_MAX]
    if due_at is not None:
        fields["due_at"] = due_at
        fields["notified_at"] = None
    if fields:
        _write_open(followup, fields)
        _announce(followup)


def close(followup, *, by=None, reason: str = "done", strict: bool = False) -> bool:
    """Done or Not needed (``reason="cancelled"``). The row is kept either way.

    Returns whether this call closed it. With ``strict`` (a person pressing a button), a
    follow-up someone else already closed raises ``FollowUpClosed`` so they're told so.
    """
    try:
        _write_open(
            followup,
            {"done_at": timezone.now(), "closed_reason": reason, "done_by": by},
        )
    except FollowUpClosed:
        if strict:
            raise
        return False
    _announce(followup)
    return True


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
        from apps.conversations.followup_copy import ALREADY_OPEN

        raise ValueError(ALREADY_OPEN)
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


# ── What's waiting on the team: one count and one grouping (interaction contract §2-§5) ──

SECTION_ORDER = ("waiting", "due", "coming_up")


def _open_followups(account):
    from apps.conversations.models import FollowUp
    from apps.core.channels import hide_disabled_channels

    return hide_disabled_channels(
        FollowUp.objects.filter(account=account, done_at__isnull=True),
        field="conversation__channel",
    )


def _mine_q(user):
    from django.db.models import Q

    return Q(assigned_to=user) | Q(assigned_to__isnull=True)


def due_customers(account, *, user=None, now=None):
    """The customers with a follow-up due now or overdue: open (not done, not cancelled),
    on a channel that's switched on, and, with ``user``, theirs or unassigned.

    The one definition behind every count. They are the customers in Waiting and Due on
    the Follow-ups page, so a count and the page can't describe different work.
    """
    now = now or timezone.now()
    qs = _open_followups(account).filter(due_at__lte=now)
    if user is not None:
        qs = qs.filter(_mine_q(user))
    # order_by() clears the model's default ordering, which DISTINCT would otherwise
    # include (contact + due time), listing a customer with two due follow-ups twice.
    return qs.order_by().values_list("contact_id", flat=True).distinct()


def due_for(user, account, *, now=None) -> int:
    """Customers with a follow-up due now or overdue for ``user`` (theirs or unassigned).

    The one count behind the nav badge, the dashboard tile and Business Health. It counts
    customers, not rows: two follow-ups for one customer are one piece of work.
    """
    return due_customers(account, user=user, now=now).count()


def due_for_team(account, *, now=None) -> int:
    """Customers with a follow-up due now or overdue for anyone in the business."""
    return due_customers(account, now=now).count()


def section_for(followup, now) -> str:
    """Waiting (a customer who wrote and got no reply), Due, or Coming up.

    A missed-chat follow-up someone moved Later waits in Coming up like any other.
    """
    if followup.due_at > now:
        return "coming_up"
    if followup.source == "missed":
        return "waiting"
    return "due"


def _urgency(followup, now):
    return (SECTION_ORDER.index(section_for(followup, now)), followup.due_at)


class FollowUpRow:
    """One customer on the Follow-ups page: their most urgent follow-up leads the row."""

    def __init__(self, contact, primary, others, section):
        self.contact = contact
        self.primary = primary
        self.others = others
        self.section = section

    @property
    def main_action(self) -> str:
        """``"reply"`` (open the chat, ready to answer) or ``"open"``. Never completes."""
        return "reply" if self.section == "waiting" else "open"

    @property
    def target_url(self) -> str:
        return target_url(self.primary, reply=self.main_action == "reply")


def target_url(followup, *, reply: bool = False) -> str:
    """Where acting on a follow-up starts: its chat (ready to answer when ``reply``), or the
    customer's page when it has no chat."""
    from django.urls import reverse

    if followup.conversation_id:
        url = reverse("conversations:detail", args=[followup.conversation.public_id])
        return url + ("?reply=1" if reply else "")
    return reverse("contacts:detail", args=[followup.contact.public_id])


def rows_for(user, account, *, scope: str = "mine", contact=None, now=None):
    """The Follow-ups page as rows, grouped by section, one row per customer.

    A customer qualifies for ``mine`` when one of their open follow-ups is theirs or
    unassigned, and for ``team`` when they have any. Once in, the row carries every open
    follow-up of theirs (each shows its real owner); the most urgent qualifying one decides
    the section and the main action. Identity is the contact, never a name or a phone.
    """
    now = now or timezone.now()
    base = _open_followups(account)
    if contact is not None:
        base = base.filter(contact=contact)
    qualifying = base.filter(_mine_q(user)) if scope == "mine" else base
    contact_ids = set(qualifying.values_list("contact_id", flat=True))
    items = (
        base.filter(contact_id__in=contact_ids)
        .select_related("contact", "conversation", "assigned_to", "created_by")
        .order_by("due_at", "pk")
    )
    groups: dict = {}
    for f in items:
        key = f.contact_id or ("conversation", f.conversation_id)
        groups.setdefault(key, []).append(f)

    def eligible(f) -> bool:
        return scope != "mine" or f.assigned_to_id in (None, user.pk)

    sections: dict[str, list[FollowUpRow]] = {name: [] for name in SECTION_ORDER}
    for group in groups.values():
        candidates = [f for f in group if eligible(f)] or group
        primary = min(candidates, key=lambda f: _urgency(f, now))
        others = sorted(
            (f for f in group if f.pk != primary.pk), key=lambda f: _urgency(f, now)
        )
        section = section_for(primary, now)
        sections[section].append(FollowUpRow(primary.contact, primary, others, section))
    for rows in sections.values():
        rows.sort(key=lambda r: (r.primary.due_at, r.primary.pk))
    return sections


def for_customer(account, contact, *, conversation=None, now=None) -> list:
    """A customer's open follow-ups as every customer-level surface shows them (the chat,
    the contact page, the lead page): ``conversation``'s own first when given, then the
    rest in the page's order of urgency. Nothing is cut off; the first one leads."""
    now = now or timezone.now()
    items = (
        _open_followups(account)
        .filter(contact_id=contact.pk)
        .select_related("contact", "conversation", "assigned_to", "created_by")
    )
    first = conversation.pk if conversation is not None else None
    return sorted(
        items,
        key=lambda f: (
            first is not None and f.conversation_id != first,
            *_urgency(f, now),
        ),
    )


def for_chat(conversation, *, now=None) -> list:
    """The customer's open follow-ups as a chat shows them: this chat's own first."""
    return for_customer(
        conversation.account, conversation.contact, conversation=conversation, now=now
    )


def latest_chat(contact):
    """The customer's most recent chat on a channel that's switched on, or None. A follow-up
    set from their contact page belongs to it, so Open chat has somewhere to go."""
    from django.db.models import F

    from apps.conversations.models import Conversation
    from apps.core.channels import hide_disabled_channels

    return (
        hide_disabled_channels(Conversation.objects.filter(contact=contact))
        .order_by(F("last_message_at").desc(nulls_last=True), "-pk")
        .first()
    )


def card(f, viewer, *, now, zone, reply: bool = False) -> dict:
    """One follow-up as every surface shows it (the Follow-ups page, the composer, the side
    panel, the contact and lead pages): the same why, when, note, owner and chat."""
    from apps.conversations import followup_copy as copy

    return {
        "followup": f,
        "why": copy.why_label(f, viewer),
        "badge": "badge-warning" if f.source == "missed" else "badge-neutral",
        "when": copy.when_label(f, now=now, zone=zone),
        "note": copy.note_for(f),
        "late": f.due_at <= now,
        "owner": copy.owner_label(f, viewer),
        "channel": copy.channel_label(f),
        "url": target_url(f, reply=reply),
    }


def customer_panel(account, contact, viewer, *, chat=None, now=None) -> dict:
    """The Follow-ups card on a customer's contact or lead page: every open follow-up of
    theirs as a card, the link to the page filtered to them, and the chat a new one would
    belong to (None: it opens their customer page instead)."""
    from django.urls import reverse

    from apps.conversations import followup_copy as copy

    now = now or timezone.now()
    zone = business_zone(account)
    name = contact.display_name
    return {
        "contact": contact,
        "chat": chat,
        "items": [
            card(f, viewer, now=now, zone=zone)
            for f in for_customer(account, contact, now=now)
        ],
        "all_url": reverse("conversations:followups_due")
        + f"?list=team&contact={contact.public_id}",
        "all_label": copy.all_for(name),
        "empty": copy.no_followups_for(name),
        "later_choices": LATER_CHOICES,
        "copy": copy,
    }


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
    from apps.conversations import followup_copy as copy

    lines = [
        f"• {f.contact.display_name}: "
        + (" / ".join(f.note.splitlines()) if f.note else copy.why_label(f))
        + f" (due {local_label(f.due_at, f.account)}, {copy.channel_label(f)})"
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
            text=copy.EMAIL_INTRO + "\n\n" + "\n".join(lines),
            path=reverse("conversations:followups_due") + "?list=mine",
            reason=copy.EMAIL_REASON,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "could not email due follow-ups to user %s", first.assigned_to_id
        )


def _can_skip_locked() -> bool:
    from django.db import connection

    return connection.features.has_select_for_update_skip_locked

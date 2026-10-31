"""Every word a business owner reads about follow-ups.

The single source for follow-up labels and messages (docs/design/followups-interaction-
contract.md). Templates and views take their wording from here and never define their own,
so "Follow-up" means one thing everywhere and the banned words (Reminder, Snooze, Remove,
Move, "list", policy, source) can't creep back in.
"""

from __future__ import annotations

# ── The promise ──────────────────────────────────────────────────────────────────────────

INTERNAL_ONLY = "Only your team sees this. Nothing is sent to the customer."
INTERNAL_ONLY_SHORT = "Only your team sees this"
NO_CHAT_NOTE = "They have no chat yet, so it will open their customer page."
# The "Email me" switch and the emails: exactly what happens (followups.notify_due). Only
# follow-ups given to you are emailed, at most once an hour; unassigned ones and customers
# waiting for a reply are not; the customer never gets anything.
EMAILS_TOGGLE_LABEL = "Email me when follow-ups given to me are due:"
EMAILS_ON = (
    "We'll email you when follow-ups given to you are due, at most once an hour. "
    "Whole-team follow-ups and customers waiting for a reply aren't emailed."
)
EMAILS_OFF = (
    "Follow-up emails are off. Yours still show here, with the count beside "
    "Follow-ups in the menu."
)
EMAIL_INTRO = "These follow-ups given to you are due now:"
EMAIL_REASON = (
    "You're getting this because these follow-ups are given to you in Akilent. "
    "Only your team gets these emails; customers never do. "
    "Turn them off on the Follow-ups page."
)
CHOOSE_WHEN = "Choose when it's due."
EMAIL_NOTE = "Your team may get an email about it; the customer never does."

# ── The page ─────────────────────────────────────────────────────────────────────────────

PAGE_TITLE = "Follow-ups"
PAGE_INTRO = "Customers your team needs to get back to."
TAB_MINE = "Mine"
TAB_TEAM = "Whole team"

WAITING = "waiting"
DUE = "due"
COMING_UP = "coming_up"

SECTION_TITLES = {
    WAITING: "Waiting for your reply",
    DUE: "Due",
    COMING_UP: "Coming up",
}
EMPTY_STATES = {
    WAITING: "No customers waiting for a reply.",
    DUE: "Nothing due. You're all caught up.",
    COMING_UP: "Nothing scheduled yet.",
}

# ── Actions ──────────────────────────────────────────────────────────────────────────────

REPLY_NOW = "Reply now"
OPEN_CHAT = "Open chat"
DONE = "Done"
LATER = "Later"
NOT_NEEDED = "Not needed"
GIVE_TO = "Give to…"
EDIT_NOTE = "Edit note"
SET_FOLLOWUP = "Set follow-up"
FOLLOW_UP_TOMORROW = "Follow up tomorrow"
CHANGE = "Change"
PICK_A_DATE = "Pick a date"
NOT_NEEDED_CONFIRM = "Cancel this follow-up? It stays in the history."

# ── What happened (one message per action) ───────────────────────────────────────────────

MARKED_DONE = "Follow-up marked as done."
CANCELLED = "Follow-up cancelled."
NOTE_UPDATED = "Note updated."
REOPENED = "Follow-up reopened."
ALREADY_DONE = "This follow-up was already done."
ALREADY_OPEN = "This chat already has an open follow-up like this one."
ASSIGNED_TO_TEAM = "Follow-up assigned to the whole team."
NOT_ON_TEAM = "That person isn't on your team."


def moved_to(when: str) -> str:
    return f"Follow-up moved to {when}."


def set_for(when: str) -> str:
    return f"Follow-up set for {when}."


def kept_existing(when: str, note_result: str = "") -> str:
    """The follow-up already there was kept; what became of the note given with the new one
    (``followups.append_note``) is said, never left to guess."""
    message = f"This chat already has a follow-up on {when}."
    if note_result == "added":
        return f"{message} Your note was added to it."
    if note_result == "already_there":
        return f"{message} It already has that note."
    if note_result == "too_long":
        return (
            f"{message} Your note wasn't added: a note holds 255 characters. "
            "Use Edit note to shorten it."
        )
    return message


def assigned_to(name: str) -> str:
    return f"Follow-up assigned to {name}."


def due_count(n: int, *, team: bool = False) -> str:
    noun = "follow-up" if n == 1 else "follow-ups"
    return f"{n} {noun} due {'for the whole team' if team else 'for you'}"


# ── Why it's here ────────────────────────────────────────────────────────────────────────

WAITING_LABEL = "Waiting for your reply"
FROM_AUTOMATION = "From an automation"
YOU_SET_THIS = "You set this"
SUGGESTED = "Suggested follow-up"


def person_name(user) -> str:
    return (user.get_full_name() or user.email) if user is not None else ""


def why_label(followup, viewer=None) -> str:
    """Why this follow-up exists, in the owner's words."""
    source = followup.source
    if source == "missed":
        return WAITING_LABEL
    if source in ("workflow", "policy"):
        return FROM_AUTOMATION
    creator = followup.created_by
    if creator is None or (viewer is not None and creator.pk == viewer.pk):
        return YOU_SET_THIS
    return f"{person_name(creator)} set this"


def _span(delta) -> str:
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h" if hours < 48 else f"{hours // 24} days"


def when_label(followup, *, now, zone) -> str:
    """When, in plain words: "waiting 26h", "2h late", "due now", "today 14:00",
    "tomorrow 09:00", "Tue 4 Nov, 09:00". Times are the business's own."""
    if followup.due_at <= now:
        if followup.source == "missed":
            convo = followup.conversation
            since = (convo.last_message_at if convo else None) or followup.created_at
            return f"for {_span(now - since)}"  # reads on from "Waiting for your reply"
        late = now - followup.due_at
        return "due now" if late.total_seconds() < 60 * 15 else f"{_span(late)} late"
    local, local_now = followup.due_at.astimezone(zone), now.astimezone(zone)
    days = (local.date() - local_now.date()).days
    clock = f"{local:%H:%M}"
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"tomorrow {clock}"
    return f"{local:%a} {local.day} {local:%b}, {clock}"


def note_for(followup) -> str:
    """The note worth showing: a person's words. The missed-chat job's own note only repeats
    "Waiting for your reply", so it isn't shown."""
    return "" if followup.source == "missed" else followup.note


def channel_label(followup) -> str:
    """Which chat it concerns, or that it has none."""
    convo = followup.conversation
    if convo is None:
        return "no chat yet: opens their customer page"
    return f"{convo.get_channel_display()} chat"


def composer_label(followup, *, now, zone) -> str:
    """The composer's one line once a follow-up exists: "Follow-up tomorrow 09:00",
    "Follow-up 3h late", or, for a customer still waiting, "Waiting for your reply · for 26h"
    (sending a reply clears that one). Never "Reminder": nothing went to the customer."""
    when = when_label(followup, now=now, zone=zone)
    if followup.source == "missed" and followup.due_at <= now:
        return f"{WAITING_LABEL} · {when}"
    return f"Follow-up {when}"


def others_label(n: int) -> str:
    return f"+{n} more for this customer"


def all_for(name: str) -> str:
    return f"All follow-ups for {name}"


def no_followups_for(name: str) -> str:
    return f"No follow-ups for {name}."


def wrote_since(name: str) -> str:
    return f"{name} wrote since this was set. Handled? Mark it done."


def inbox_tag(due_at, *, now, zone) -> str:
    """The inbox row's tag for a chat with a follow-up: "Follow-up due", "Follow-up today",
    "Follow-up tomorrow" or "Follow-up Tue 4 Nov", in the business's own time."""
    if due_at <= now:
        return "Follow-up due"
    days = (due_at.astimezone(zone).date() - now.astimezone(zone).date()).days
    if days == 0:
        return "Follow-up today"
    if days == 1:
        return "Follow-up tomorrow"
    local = due_at.astimezone(zone)
    return f"Follow-up {local:%a} {local.day} {local:%b}"


def owner_label(followup, viewer=None) -> str:
    """Who it's for: "for you", "for Joseph", or "for the team"."""
    owner = followup.assigned_to
    if owner is None:
        return "for the team"
    if viewer is not None and owner.pk == viewer.pk:
        return "for you"
    return f"for {person_name(owner)}"

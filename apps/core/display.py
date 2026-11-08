"""Small display helpers shared by the list and settings pages.

* ``when_text``: a time as people say it ("12 days ago", "yesterday", "8 Nov, 13:29"),
  in the workspace's own time zone, replacing Django's ``timesince`` ("1 week, 4 days").
* ``readable_name``: a WhatsApp template's snake_case name for reading
  ("order_update" → "Order update"). Display only: every send uses the raw name.
"""

from __future__ import annotations

import datetime
import re
from zoneinfo import ZoneInfo

RELATIVE_DAYS = 30  # after this, the date itself is the useful fact


def when_text(
    value: datetime.datetime, zone: ZoneInfo, now: datetime.datetime | None = None
) -> str:
    """ "just now", "5 min ago", "3 hours ago", "yesterday", "12 days ago", then
    "8 Nov, 13:29" (with the year when it isn't this year). Days are counted on the
    workspace's calendar, so 23:59 → 00:01 is "yesterday" there, not "2 minutes ago"."""
    from django.utils import timezone

    if not value:
        return ""
    now = now or timezone.now()
    local, local_now = value.astimezone(zone), now.astimezone(zone)
    seconds = (now - value).total_seconds()
    if seconds < 0:  # a time in the future (a clock a little ahead): the date
        return _date(local, local_now)
    if seconds < 60:
        return "just now"
    days = (local_now.date() - local.date()).days
    if days == 0:
        if seconds < 3600:
            minutes = int(seconds // 60)
            return f"{minutes} min ago"
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    if days == 1:
        return "yesterday"
    if days <= RELATIVE_DAYS:
        return f"{days} days ago"
    return _date(local, local_now)


def _date(local: datetime.datetime, local_now: datetime.datetime) -> str:
    text = f"{local.day} {local:%b}"
    if local.year != local_now.year:
        text += f" {local.year}"
    return f"{text}, {local:%H:%M}"


def full_text(value: datetime.datetime, zone: ZoneInfo) -> str:
    """The exact time for a tooltip: "Sun 8 Nov 2026, 13:29"."""
    local = value.astimezone(zone)
    return f"{local:%a} {local.day} {local:%b %Y, %H:%M}"


# Meta's own example templates every new WhatsApp Business Account has. Marked as samples
# and hidden behind "Show samples" on Message templates.
SAMPLE_TEMPLATE_NAMES = frozenset(
    {"hello_world", "3p_direct_integration_test_template"}
)


def readable_name(name: str) -> str:
    """ "order_update_v2" → "Order update v2"; a name that already has spaces or capitals
    is left as written."""
    if not name or not re.fullmatch(r"[a-z0-9_]+", name):
        return name
    words = [w for w in name.split("_") if w]
    if not words:
        return name
    text = " ".join(words)
    return text[0].upper() + text[1:]

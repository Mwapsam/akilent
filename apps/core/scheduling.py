"""Timezone + recurrence helpers shared by apps.scheduler and apps.automation.

All storage is UTC (settings.USE_TZ / TIME_ZONE="UTC"). These helpers convert a
wall-clock local time in an IANA zone to a UTC instant, and walk an RFC-5545
RRULE to the next occurrence in a DST-safe way (recompute "Mon 09:00 local" per
occurrence, never add a fixed number of hours).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as _timezone
from zoneinfo import ZoneInfo, available_timezones

# Sentinel accepted by the API in place of a concrete IANA zone: resolve the
# effective timezone per recipient at fire time (campaign / automation), or from
# the single target contact (transactional).
RECIPIENT_TZ = "recipient"

_VALID_ZONES = available_timezones()


class InvalidTimezone(ValueError):
    pass


def validate_iana(tz: str, *, allow_recipient: bool = False) -> str:
    """Return ``tz`` unchanged if it is a known IANA zone (or the recipient
    sentinel when allowed); raise InvalidTimezone otherwise."""
    if allow_recipient and tz == RECIPIENT_TZ:
        return tz
    if tz in _VALID_ZONES:
        return tz
    raise InvalidTimezone(f"{tz!r} is not a valid IANA timezone")


def to_utc(dt: datetime, tz: str) -> datetime:
    """Interpret ``dt`` as wall-clock time in ``tz`` and return the UTC instant.

    If ``dt`` is already timezone-aware its own offset wins and ``tz`` is
    ignored — this is the "ISO-8601 string with offset" path.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(_timezone.utc)
    validate_iana(tz)
    return dt.replace(tzinfo=ZoneInfo(tz)).astimezone(_timezone.utc)


def local_time_next(tz: str, hhmm: str, *, after: datetime | None = None) -> datetime:
    """Next UTC instant at which the wall clock in ``tz`` reads ``hhmm`` (``"09:00"``).

    Returns strictly-future relative to ``after`` (default: now).
    """
    validate_iana(tz)
    zone = ZoneInfo(tz)
    ref = (after or datetime.now(_timezone.utc)).astimezone(zone)
    hh, mm = (int(x) for x in hhmm.split(":"))
    candidate = ref.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= ref:
        candidate = candidate + timedelta(days=1)
        candidate = candidate.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return candidate.astimezone(_timezone.utc)


def next_occurrence(rrule: str, *, after: datetime, tz: str) -> datetime | None:
    """Walk ``rrule`` (RFC-5545) to the first occurrence strictly after ``after``.

    ``after`` is a UTC instant; the rule is evaluated in ``tz`` so that e.g.
    ``FREQ=WEEKLY;BYDAY=MO;BYHOUR=9`` lands on 09:00 local across DST changes.
    Returns a UTC instant, or None when the rule is exhausted.
    """
    from dateutil.rrule import rrulestr

    validate_iana(tz)
    zone = ZoneInfo(tz)
    local_after = after.astimezone(zone)
    dtstart = local_after.replace(second=0, microsecond=0)
    rule = rrulestr(rrule, dtstart=dtstart)
    nxt = rule.after(local_after, inc=False)
    if nxt is None:
        return None
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=zone)
    return nxt.astimezone(_timezone.utc)


def validate_rrule(rrule: str, *, min_interval_secs: int = 3600) -> str:
    """Validate an RRULE string and reject sub-``min_interval_secs`` cadences.

    Returns the rule unchanged on success; raises ValueError otherwise.
    """
    from dateutil.rrule import rrulestr

    now = datetime.now(_timezone.utc)
    try:
        rule = rrulestr(rrule, dtstart=now)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid RRULE: {exc}") from exc

    first = rule.after(now, inc=False)
    second = rule.after(first, inc=False) if first else None
    if first and second and (second - first).total_seconds() < min_interval_secs:
        raise ValueError(
            f"recurrence interval must be at least {min_interval_secs // 60} minutes apart"
        )
    return rrule

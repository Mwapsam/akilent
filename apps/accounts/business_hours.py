"""Opening hours: "are we open right now?" for a business, in its own timezone.

Deterministic and fails open. A business that has not set hours is always "open", so no
automation ever tells a customer "we're closed" by accident. One window per day (open
before close, no overnight windows): simple to explain, and enough for the shops, clinics
and restaurants this is built for. A night-shift business can leave the day open all day.
"""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo, available_timezones

from django.utils import timezone

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_LABELS = {
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday",
    "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}
DEFAULT_TIMEZONE = "UTC"
DEFAULT_WINDOW = {"open": "09:00", "close": "17:00"}


class HoursError(ValueError):
    """A problem with submitted hours, worded for a business owner."""


def parse_time(value) -> time:
    try:
        hours, minutes = str(value).strip().split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        raise HoursError("Enter times like 09:00.") from None


def clean_timezone(name: str) -> str:
    name = (name or "").strip()
    if name not in available_timezones():
        raise HoursError("Choose your timezone from the list.")
    return name


def clean_schedule(raw: dict | None) -> dict:
    """Validate a ``{day: {"open": "HH:MM", "close": "HH:MM"}}`` schedule; drop closed days."""
    clean = {}
    for day, window in (raw or {}).items():
        if day not in DAYS:
            raise HoursError(f"{day!r} is not a day of the week.")
        if not window:
            continue
        opens, closes = parse_time(window.get("open")), parse_time(window.get("close"))
        if closes <= opens:
            raise HoursError(f"{DAY_LABELS[day]}: closing time must be after opening time.")
        clean[day] = {"open": opens.strftime("%H:%M"), "close": closes.strftime("%H:%M")}
    return clean


COMMON_TIMEZONES = [
    "Africa/Lusaka", "Africa/Harare", "Africa/Johannesburg", "Africa/Nairobi", "Africa/Lagos",
    "Africa/Accra", "Africa/Cairo", "Europe/London", "UTC",
]


def timezone_choices() -> list[str]:
    """Common African zones first, then every other zone."""
    return COMMON_TIMEZONES + sorted(z for z in available_timezones() if z not in COMMON_TIMEZONES)


def form_rows(hours) -> list[dict]:
    """One form row per weekday. A business with no hours yet sees Monday to Friday, 9 to 5,
    pre-filled so the common case is one click; nothing is stored until they save."""
    saved = hours.schedule if hours and hours.schedule else None
    rows = []
    for day in DAYS:
        window = (saved or {}).get(day) if saved else (DEFAULT_WINDOW if day not in ("sat", "sun") else None)
        window = window or DEFAULT_WINDOW
        is_open = bool((saved or {}).get(day)) if saved else day not in ("sat", "sun")
        rows.append({"key": day, "label": DAY_LABELS[day], "open": is_open,
                     "from": window["open"], "to": window["close"]})
    return rows


def schedule_from_form(data) -> dict:
    """The schedule from the hours form (``<day>_open`` / ``<day>_from`` / ``<day>_to`` fields)."""
    return {
        day: {"open": data.get(f"{day}_from", ""), "close": data.get(f"{day}_to", "")}
        for day in DAYS if data.get(f"{day}_open") == "on"
    }


def get_hours(account):
    from apps.accounts.models import BusinessHours

    return BusinessHours.objects.filter(account=account).first()


def is_configured(account) -> bool:
    hours = get_hours(account)
    return bool(hours and hours.schedule)


def save_hours(account, *, tz: str, schedule: dict | None):
    """Validate and store a business's hours. An empty schedule means "always open"."""
    from apps.accounts.models import BusinessHours

    hours, _ = BusinessHours.objects.update_or_create(
        account=account,
        defaults={"timezone": clean_timezone(tz), "schedule": clean_schedule(schedule)},
    )
    return hours


def describe(account) -> str:
    """The week in words, days with the same hours grouped: "Monday to Friday 08:00-17:00,
    Saturday 09:00-13:00". "" when no hours are set."""
    hours = get_hours(account)
    if hours is None or not hours.schedule:
        return ""
    runs: list[list] = []  # [first_day, last_day, window]
    for day in DAYS:
        window = hours.schedule.get(day)
        key = f"{window['open']}-{window['close']}" if window else None
        if runs and runs[-1][2] == key and DAYS.index(runs[-1][1]) == DAYS.index(day) - 1:
            runs[-1][1] = day
        else:
            runs.append([day, day, key])
    parts = []
    for first, last, key in runs:
        if key is None:
            continue
        days = DAY_LABELS[first] if first == last else f"{DAY_LABELS[first]} to {DAY_LABELS[last]}"
        parts.append(f"{days} {key}")
    return ", ".join(parts)


def availability(account, at: datetime | None = None) -> dict:
    """"Are we open now, and if not, when?" in words a reply can repeat.

    ``{"hours_set", "open_now", "local_time", "today", "next_open", "timezone"}``. With no hours set
    the business counts as open (see ``is_open``) and the rest is empty.
    """
    hours = get_hours(account)
    if hours is None or not hours.schedule:
        return {"hours_set": False, "open_now": True, "local_time": "", "today": "", "next_open": "", "timezone": ""}
    try:
        zone = ZoneInfo(hours.timezone)
    except Exception:
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    local = (at or timezone.now()).astimezone(zone)
    today = hours.schedule.get(DAYS[local.weekday()])
    open_now = is_open(account, at)
    next_open = ""
    if not open_now:
        for ahead in range(8):
            day = DAYS[(local.weekday() + ahead) % 7]
            window = hours.schedule.get(day)
            if not window or (ahead == 0 and local.time() >= parse_time(window["open"])):
                continue
            when = "today" if ahead == 0 else ("tomorrow" if ahead == 1 else DAY_LABELS[day])
            next_open = f"{when} at {window['open']}"
            break
    return {
        "hours_set": True, "open_now": open_now, "local_time": local.strftime("%A %H:%M"),
        "today": f"{today['open']}-{today['close']}" if today else "closed",
        "next_open": next_open, "timezone": hours.timezone,
    }


def is_open(account, at: datetime | None = None) -> bool:
    """Whether the business is open at ``at`` (default now). True when no hours are set."""
    return open_in(get_hours(account), at)


def open_in(hours, at: datetime | None = None) -> bool:
    """``is_open`` for an already-loaded ``BusinessHours`` (or None): no query, for loops."""
    if hours is None or not hours.schedule:
        return True
    try:
        zone = ZoneInfo(hours.timezone)
    except Exception:  # an unknown stored zone must not stop replies; fall back to UTC
        zone = ZoneInfo(DEFAULT_TIMEZONE)
    local = (at or timezone.now()).astimezone(zone)
    window = hours.schedule.get(DAYS[local.weekday()])
    if not window:
        return False
    return parse_time(window["open"]) <= local.time() < parse_time(window["close"])

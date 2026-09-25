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
    hours = get_hours(account)
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

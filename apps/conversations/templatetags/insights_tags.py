"""Template filters for the Business Health page. See ``apps.conversations.reporting``."""

from __future__ import annotations

from django import template

from apps.conversations import reporting

register = template.Library()


@register.filter
def duration(seconds):
    """Seconds as "45s", "2m 14s", "3h 5m"; "—" for None. See ``reporting.duration``."""
    return reporting.duration(seconds)


@register.filter
def duration_minutes(minutes):
    """Minutes as a duration string ("45m", "3h 5m"); see ``duration``."""
    return reporting.duration(None if minutes is None else minutes * 60)


@register.filter
def hour12(hour: int) -> str:
    """0 -> "12am", 13 -> "1pm", 18 -> "6pm"."""
    hour = int(hour)
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12 or 12
    return f"{display}{suffix}"

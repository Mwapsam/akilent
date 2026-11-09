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
def share(part, whole):
    """A rate that's honest about its sample: "73%" from enough, "1 of 1" from a handful
    (below ``reporting.MIN_SAMPLE``), "—" from nothing. Never "100%" from one conversation."""
    part, whole = int(part or 0), int(whole or 0)
    if not whole:
        return "—"
    if whole < reporting.MIN_SAMPLE:
        return f"{part} of {whole}"
    return f"{round(part / whole * 100)}%"


@register.filter
def figure(value, empty="Not enough data"):
    """A figure's value, or muted words in place of a bare "—" (2026-11-09 review):
    ``{{ x|share:y|figure }}``, ``{{ s|duration|figure }}``."""
    from django.utils.html import format_html

    if value in (None, "", "—"):
        return format_html('<span class="metric-empty">{}</span>', empty)
    return value


@register.filter
def thin(n) -> bool:
    """Something to show, but too little to call it a rate or a trend yet."""
    n = int(n or 0)
    return 0 < n < reporting.MIN_SAMPLE


@register.filter
def hour12(hour: int) -> str:
    """0 -> "12am", 13 -> "1pm", 18 -> "6pm"."""
    hour = int(hour)
    suffix = "am" if hour < 12 else "pm"
    display = hour % 12 or 12
    return f"{display}{suffix}"

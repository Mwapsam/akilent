"""Removable chips for the filters in use (Content Studio app feel, Phase 3).

Each chip says what a filter is set to and links to the same page without it, so it works
without JavaScript; with it, app.js clears the field and applies the live filter instead
(templates/publishing/_filter_chips.html).
"""

from __future__ import annotations


def date_range_label(date_from: str, date_to: str, today) -> str:
    """How a date filter reads: the preset it matches ("Last 7 days", "This month"), else
    the days ("1 Oct – 31 Oct", "From 1 Oct", "Until 31 Oct"). The Library's chip and its
    date trigger both show it; picking a preset sets the same words in the page at once."""
    import datetime

    def day(raw):
        try:
            return datetime.date.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    start, end = day(date_from), day(date_to)
    if not start and not end:
        return "Any time"
    if end == today and start:
        for days in (7, 30, 90):
            if start == today - datetime.timedelta(days=days - 1):
                return f"Last {days} days"
        if start == today.replace(day=1):
            return "This month"

    def fmt(d):
        return f"{d.day} {d:%b}" + (f" {d.year}" if d.year != today.year else "")

    if start and end:
        return f"{fmt(start)} \N{EN DASH} {fmt(end)}"
    return f"From {fmt(start)}" if start else f"Until {fmt(end)}"


def chips(request, active: list[tuple[tuple[str, ...], str]]) -> list[dict]:
    """``active``: (the query parameters a filter uses, what to call it) for each filter
    that is set. Returns ``label``, ``url`` (this page without them, back to page 1) and
    ``clear`` (the field names, for app.js)."""
    out = []
    for names, label in active:
        query = request.GET.copy()
        for name in (*names, "page"):
            query.pop(name, None)
        rest = query.urlencode()
        out.append(
            {
                "label": label,
                "url": f"{request.path}?{rest}" if rest else request.path,
                "clear": " ".join(names),
            }
        )
    return out

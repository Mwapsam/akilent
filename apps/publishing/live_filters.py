"""Removable chips for the filters in use (Content Studio app feel, Phase 3).

Each chip says what a filter is set to and links to the same page without it, so it works
without JavaScript; with it, app.js clears the field and applies the live filter instead
(templates/publishing/_filter_chips.html).
"""

from __future__ import annotations


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

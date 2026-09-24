"""Every table in the app shell must reflow or scroll - never clip.

`.table-wrap` used to be `overflow-hidden`, so a table wider than its container
was silently cut off with no scrollbar. It now scrolls, but a table that sits in
no wrapper at all and does not opt into `.table--cards` still overflows the
page, which breaks spec §11 ("no horizontal page scroll at any width").

A table satisfies this if either:
  * it carries `.table--cards`, so rows stack into cards below md, or
  * it is inside something that scrolls - `.table-wrap`, `.table-scroll`, or a
    bare `overflow-x-auto`.

The invariant is "does not clip", not "uses our class names", so a hand-rolled
`overflow-x-auto` passes. (core/configurations_list.html is the one place that
does it that way, with a bare <table> rather than `.table`.)
"""

import re
from pathlib import Path

from django.conf import settings

TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"
TABLE_TAG = re.compile(r"<table\b[^>]*>")
# How far back to look for the wrapper that opens around the table.
LOOKBEHIND = 400


def _app_templates():
    for path in TEMPLATES_DIR.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        if 'extends "base.html"' not in source:
            continue
        yield str(path.relative_to(TEMPLATES_DIR)).replace("\\", "/"), source


def test_no_table_can_clip():
    offenders = {}
    for rel, source in _app_templates():
        for match in TABLE_TAG.finditer(source):
            tag = match.group(0)
            if "table--cards" in tag:
                continue
            before = source[max(0, match.start() - LOOKBEHIND) : match.start()]
            if any(w in before for w in ("table-wrap", "table-scroll", "overflow-x-auto")):
                continue
            line = source[: match.start()].count("\n") + 1
            offenders.setdefault(rel, []).append(f"line {line}")
    assert not offenders, (
        "These tables neither reflow (.table--cards) nor scroll "
        f"(.table-wrap / .table-scroll), so they clip: {offenders}"
    )

"""Tables on the docs and help pages, which the app-shell test cannot see.

``test_responsive_tables.py`` covers every template that extends base.html, and
covers it better than a whole-file grep could: it looks only at the markup
immediately around each ``<table>``, so a page with one wrapped table and one
bare one is still caught.

Docs and help articles extend neither base.html nor anything with a
``.table-wrap``. They are hand-authored ``<table>`` markup that relies entirely
on a rule in their shell:

    .docs-body table,
    .article-body table { display: block; overflow-x: auto; }

Nothing connects those pages to that rule, so deleting it would quietly bring
back the clipping that spec §11 forbids, on every table in the documentation at
once. That is what this file pins.
"""

import re
from pathlib import Path

from django.conf import settings

TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"

# shell template -> (pages it wraps, the class its body carries)
SHELLS = {
    "docs/_layout.html": ("docs/", "docs-body"),
    "help/article.html": ("help/", "article-body"),
}


def _scrolls(css: str, body_class: str) -> bool:
    """Both halves of the rule, not just one.

    ``overflow-x: auto`` does nothing on a ``display: table`` element — the box
    sizes itself to its content and overflows its parent instead of scrolling.
    A shell that kept the overflow and lost the display change would read as
    protected while clipping exactly as before.
    """
    rule = re.search(r"\.%s table\s*{([^}]*)}" % re.escape(body_class), css)
    if not rule:
        return False
    return bool(
        re.search(r"overflow-x\s*:\s*auto", rule.group(1))
        and re.search(r"display\s*:\s*block", rule.group(1))
    )


def test_the_docs_and_help_shells_keep_their_tables_scrollable():
    for shell, (_, body_class) in SHELLS.items():
        css = (TEMPLATES_DIR / shell).read_text(encoding="utf-8")
        assert _scrolls(css, body_class), (
            "%s no longer makes its tables scroll, so every table on the pages "
            "it wraps now clips" % shell
        )


def test_every_page_with_a_table_is_covered_by_a_shell_or_its_own_wrapper():
    """Catches a docs or help page that stops extending its shell, or a new
    top-level page that belongs to neither this test nor the app-shell one."""
    local = ("table--cards", "table-wrap", "table-scroll", "overflow-x-auto")
    offenders = []
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        source = path.read_text(encoding="utf-8")
        if "<table" not in source:
            continue
        rel = str(path.relative_to(TEMPLATES_DIR)).replace("\\", "/")
        if 'extends "base.html"' in source:
            continue  # test_responsive_tables.py owns these, in more detail
        if any(marker in source for marker in local):
            continue
        covered = any(
            rel.startswith(prefix)
            and _scrolls((TEMPLATES_DIR / shell).read_text(encoding="utf-8"), body_class)
            for shell, (prefix, body_class) in SHELLS.items()
        )
        if not covered:
            offenders.append(rel)
    assert not offenders, (
        "these pages render a table that can neither reflow nor scroll: %s" % offenders
    )

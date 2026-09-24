"""Every table must either reflow into cards or scroll — never clip.

The original defect (UI plan A1/A2) was silent: `.table-wrap` was
`overflow-hidden`, so a table wider than its column was simply cut off with no
scrollbar and nothing to indicate that columns were missing. It is the kind of
thing that only shows up on a device you don't own, which is why it is a test.
"""
import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"


# Handled by the template's own wrapper or utility.
LOCAL_MARKERS = ("table--cards", "table-wrap", "table-scroll", "overflow-x-auto")

# Handled by the shell the page renders inside: both set `display:block;
# overflow-x:auto` on every descendant table, so the page itself needs nothing.
SHELL_BODIES = {
    "docs/_layout.html": ("docs/", "docs-body"),
    "help/article.html": ("help/", "article-body"),
}


def _scrollable_table_rule(css: str, body_class: str) -> bool:
    """Both halves of the rule, not just one.

    ``overflow-x: auto`` does nothing on a ``display: table`` element — the
    box sizes itself to its content and overflows its parent instead of
    scrolling. So a shell that kept the overflow and lost the display change
    would read as protected while clipping exactly as before.
    """
    rule = re.search(r"\.%s table\s*{([^}]*)}" % re.escape(body_class), css)
    if not rule:
        return False
    body = rule.group(1)
    return bool(
        re.search(r"overflow-x\s*:\s*auto", body)
        and re.search(r"display\s*:\s*block", body)
    )


def _shell_covers(rel: str) -> bool:
    for shell, (prefix, body_class) in SHELL_BODIES.items():
        if not rel.startswith(prefix):
            continue
        css = (TEMPLATES / shell).read_text(encoding="utf-8")
        if _scrollable_table_rule(css, body_class):
            return True
    return False


def test_no_table_can_clip():
    offenders = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        rel = path.relative_to(TEMPLATES).as_posix()
        source = path.read_text(encoding="utf-8")
        if "<table" not in source:
            continue
        if any(marker in source for marker in LOCAL_MARKERS):
            continue
        if _shell_covers(rel):
            continue
        offenders.append(rel)
    assert not offenders, (
        "these templates render a table that can neither reflow nor scroll, so "
        "it clips on a narrow viewport: %s" % offenders
    )


def test_the_shell_rule_the_docs_and_help_pages_rely_on_is_really_there():
    """Guards the exemption above. If someone removes the overflow rule from a
    shell, the pages inside it start clipping and the test above would still
    pass by way of an exemption that no longer means anything."""
    for shell, (_, body_class) in SHELL_BODIES.items():
        css = (TEMPLATES / shell).read_text(encoding="utf-8")
        assert _scrollable_table_rule(css, body_class), (
            "%s no longer makes its tables scrollable, so every table on the "
            "pages it wraps now clips" % shell
        )

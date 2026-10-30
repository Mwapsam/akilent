"""Phase 9 of the UI quality programme (docs/design/ui-quality-log.md, Entry 16): the public pages,
the email pages, the logs and the Operator Console are on the tokens, keyboard users start at the
skip link, and small targets have the 24px minimum."""

import json
from pathlib import Path

import pytest
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
TEMPLATES = ROOT / "templates"


def _read(*parts):
    return (ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_the_drift_baseline_is_zero_everywhere():
    baseline = json.loads(_read("apps", "core", "tests", "drift_baseline.json"))
    assert sum(baseline.values()) == 0, baseline


def test_code_blocks_have_their_own_fixed_tokens():
    css = _read("assets", "app.css")
    for token in ("--code-bg:", "--code-fg:", "--code-line:"):
        assert token in css
    docs = _read("templates", "docs", "_layout.html")
    assert "background:var(--code-bg)" in docs


def test_the_help_search_does_not_take_focus_on_load():
    # Autofocus put keyboard and screen-reader users mid-page, past the skip link and header.
    assert "autofocus" not in _read("templates", "help", "index.html")


@pytest.mark.django_db
def test_the_unsubscribe_page_follows_the_reader_theme(client):
    html = client.get("/email/t/unsub/not-a-real-token/").content.decode()
    assert "css/app.css" in html
    assert "var(--tone-coral-bg)" in html
    assert "#f8d7da" not in html


def test_small_targets_get_the_minimum():
    css = _read("assets", "app.css")
    assert ":is(.table, .table-wrap table) :is(td, th) a:not(.btn)" in css
    assert "min-h-6" in _read("templates", "components", "help_link.html")
    assert (
        ".pub-signin{ display:inline-flex; align-items:center; min-height:1.5rem;"
        in _read("templates", "public", "_base.html")
    )
    assert ".docs-body :is(td, th) a{" in _read("templates", "docs", "_layout.html")


def test_the_console_settings_page_fits_a_phone():
    html = _read("templates", "manage", "settings.html")
    assert '<div class="grid grid-cols-1 lg:grid-cols-3 gap-6">' in html
    assert "min-w-0 w-full min-h-6" in html  # the logo file input can shrink


# ---- found by the full regression crawl at 360px -------------------------------------------


def test_no_field_shrinks_its_text_below_16px_on_a_phone():
    """The shared fields are text-base on phones (iOS zooms the page on anything smaller) and
    text-sm from sm up. An unprefixed text-sm/text-xs on a field undid that everywhere."""
    import re

    field = re.compile(r'<(?:input|textarea|select)\b[^>]*?class="([^"]*)"', re.S)
    for path in TEMPLATES.rglob("*.html"):
        for cls in field.findall(path.read_text(encoding="utf-8")):
            toks = cls.split()
            if any(t in ("input", "textarea", "select") for t in toks):
                assert "text-sm" not in toks and "text-xs" not in toks, (path, cls)


def test_action_rows_wrap_on_a_phone():
    for parts in (("insights", "_intelligence.html"), ("instagram", "accounts.html")):
        html = _read("templates", *parts)
        assert (
            '<div class="flex items-center gap-2 min-w-0 max-w-full flex-wrap">' in html
        )
        assert '<div class="flex items-center gap-2 shrink-0 flex-wrap">' not in html
    assert "grid-template-columns:minmax(0,1fr)" in _read(
        "templates", "help", "_styles.html"
    )


def test_fields_found_unlabelled_have_labels():
    assert 'for="payment-other"' in _read("templates", "automation", "build.html")
    assert 'for="asset-search"' in _read("templates", "email", "assets.html")


def test_script_built_fields_keep_16px_on_a_phone():
    import re

    built = re.compile(r'className\s*=\s*["\']([^"\']*)["\']')
    for path in (ROOT / "static" / "js").glob("*.js"):
        for cls in built.findall(path.read_text(encoding="utf-8")):
            toks = cls.split()
            if any(t in ("input", "textarea", "select") for t in toks):
                assert "text-sm" not in toks and "text-xs" not in toks, (path, cls)


def test_the_inbox_list_column_contains_its_absolute_children():
    """A row's .sr-only label is position:absolute. Without a positioned scroll column it escaped
    the column's scroll box, and a list longer than the screen made the whole inbox page scroll."""
    css = _read("assets", "app.css")
    i = css.index(
        ".inbox-split .inbox-list-col,\n    .inbox-split.has-conversation .inbox-list-col {"
    )
    rule = css[i : css.index("}", i)]
    assert "overflow-y: auto" in rule and "position: relative" in rule

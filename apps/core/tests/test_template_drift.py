"""Template drift guard (UI quality programme, Phase 0.10).

Two halves:

1. The detector is right. Every rule has markup it must catch and markup it must
   leave alone; a ratchet built on a detector that over- or under-counts would be
   measuring nothing.
2. The ratchet. Each templates/ directory has a count per rule in
   drift_baseline.json. It may not go up - new drift fails here - and when a fix
   brings it down the baseline must be lowered with it
   (``python -m apps.core.template_drift --write``), so the gain is locked in and
   the area sweep's "zero" is visible in review.
"""

import json

import pytest

from apps.core import template_drift as drift


def rules(src):
    return sorted({f.rule for f in drift.scan_source(src)})


# --- 1. The detector --------------------------------------------------------------

CAUGHT = [
    ('<div class="bg-[#FFB020] p-2">', "hex-class"),
    ('<p style="color:#12182B">', "hex-style"),
    ("<style>.x { color: #fff; }</style>", "hex-style"),
    ('<span class="text-[11px]">', "px-text"),
    ('<span class="text-indigo-600">', "stock-hue"),
    ('<span class="hover:bg-purple-500">', "stock-hue"),
    ('<p class="text-sm text-gray-400">', "gray-text"),
    ('<p class="text-gray-500">', "gray-text"),
    ('<div><table class="table"></table></div>', "table-unwrapped"),
    ('<div class="min-h-screen flex">', "viewport-height"),
    ('<div style="height:100vh">', "viewport-height"),
    ('<div class="md:opacity-0 md:group-hover:opacity-100">', "hover-only"),
]

LEFT_ALONE = [
    # Tokens and brand ramps are the point.
    '<div class="bg-surface text-ink-subtle border-border">',
    '<span class="text-blue-700 bg-amber-400 text-coral-700">',
    # A variant on gray is a state, not resting text (hover:text-gray-500 is rare and
    # still flagged by eye in review); the bare utility is what fails contrast.
    '<a class="hover:text-gray-400">',
    # Allowed type scale and arbitrary non-px sizes.
    '<span class="text-xs text-[0.8rem]">',
    # Wrapped, card-stacked and presentational tables.
    '<div class="table-wrap"><table class="table"></table></div>',
    '<div class="overflow-x-auto"><div><table></table></div></div>',
    '<table class="table table--cards"></table>',
    '<table role="presentation"></table>',
    # Viewport heights that follow the browser toolbar.
    '<div class="min-h-viewport">',
    "<style>.x { height: 100vh; height: 100dvh; }</style>",
    # Hover reveal that only hides for a mouse.
    '<div class="md:pointer-fine:opacity-0 md:group-hover:opacity-100">',
    # Not markup: comments, scripts and template expressions.
    '{% comment %}<p class="text-gray-400" style="color:#fff">{% endcomment %}',
    '{# <span class="text-[11px]"> #}',
    "<!-- <table></table> -->",
    '<script>el.style.color = "#fff"; el.className = "text-gray-400";</script>',
    '<span style="background: {{ pillar.color|default:"#0ea5e9" }}">',
    # A custom property that points at a token is not a raw colour.
    "<style>.cv { --cv-line: var(--color-border); }</style>",
]


@pytest.mark.parametrize("src,rule", CAUGHT)
def test_detector_catches(src, rule):
    assert rule in rules(src), f"{rule} not found in {src!r}"


@pytest.mark.parametrize("src", LEFT_ALONE)
def test_detector_leaves_alone(src):
    assert rules(src) == [], f"false positive on {src!r}: {rules(src)}"


def test_line_numbers_survive_stripping():
    src = '{% comment %}\nline 2\nline 3\n{% endcomment %}\n<p class="text-gray-400">'
    (finding,) = drift.scan_source(src, "a/b.html")
    assert finding.line == 5
    assert finding.area == "a"


def test_exceptions_are_complete_and_known():
    entries = drift.load_exceptions()
    assert entries, "the exceptions file should load"
    for e in entries:
        assert e["rule"] == "*" or e["rule"] in drift.RULES
        assert len(e["reason"]) > 20, f"give {e['path']} a reason a reviewer can check"


def test_an_exception_needs_a_reason(tmp_path):
    bad = tmp_path / "x.toml"
    bad.write_text('[[exception]]\npath = "a.html"\nrule = "hex-style"\nreason = " "\n')
    with pytest.raises(ValueError):
        drift.load_exceptions(bad)


# --- 2. The ratchet ---------------------------------------------------------------


def test_drift_does_not_grow_and_fixes_are_locked_in():
    baseline = json.loads(drift.BASELINE.read_text(encoding="utf-8"))
    current = drift.counts(drift.scan_templates())
    grew = {
        k: (baseline.get(k, 0), n) for k, n in current.items() if n > baseline.get(k, 0)
    }
    shrank = {
        k: (n, current.get(k, 0)) for k, n in baseline.items() if current.get(k, 0) < n
    }
    assert not grew, (
        "New design-system drift (area::rule: baseline -> now). Use the tokens and "
        "components in assets/app.css, or add a justified entry to "
        f"drift_exceptions.toml: {grew}"
    )
    assert not shrank, (
        "Drift went down - lock it in with `python -m apps.core.template_drift --write` "
        f"(area::rule: baseline -> now): {shrank}"
    )

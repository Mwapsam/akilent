"""Text tokens must stay legible in both themes.

The colour ramps do not flip with the theme - only the semantic tokens do. So
a token defined as a mid ramp step keeps its light-mode value on the dark
surface. `text-brand-600`, used for links in 44 templates, measured **2.05:1**
against the dark surface, and its hover step `text-brand-700` was worse at
1.52:1 - the link got *less* readable when you pointed at it. WCAG AA wants
4.5:1 for body text.

Nothing else catches this: the markup is valid, the classes exist, the build
is clean and the dark-mode token test only looks at backgrounds.
"""

import re

import pytest

from apps.core.tests.css_source import app_css_source

CSS = app_css_source()
AA_BODY_TEXT = 4.5

DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;}]+)\s*;")


def _declarations(block):
    return {m.group(1): m.group(2).strip() for m in DECL.finditer(block)}


def _themes():
    """The resolved token table for each theme.

    Light is everything declared before the dark blocks; dark is that same
    table with the [data-theme="dark"] overrides applied on top.
    """
    dark_start = CSS.index(':root[data-theme="dark"]')
    light = _declarations(CSS[: CSS.index("@media (prefers-color-scheme: dark)")])
    dark_block = CSS[dark_start:]
    dark_block = dark_block[: dark_block.index("\n}")]
    dark = dict(light)
    dark.update(_declarations(dark_block))
    return {"light": light, "dark": dark}


def _resolve(name, table, seen=None):
    """Follow var() indirection to a literal hex."""
    seen = seen or set()
    assert name not in seen, f"circular token reference at {name}"
    seen.add(name)
    value = table[name]
    ref = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if ref:
        return _resolve(ref.group(1), table, seen)
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", value), (
        f"{name} is not a plain hex: {value}"
    )
    return value


def _channel(c):
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour):
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(fg, bg):
    a, b = _luminance(fg), _luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def test_contrast_helper_matches_known_values():
    """Guard the maths itself, so a broken helper cannot pass everything."""
    assert round(contrast("#000000", "#ffffff"), 2) == 21.0
    assert round(contrast("#ffffff", "#ffffff"), 2) == 1.0


FOREGROUNDS = [
    "--color-ink",
    "--color-ink-muted",
    "--color-ink-subtle",
    "--color-accent",
    "--color-accent-strong",
]
BACKGROUNDS = ["--color-surface", "--color-canvas", "--color-surface-muted"]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_tokens_meet_aa_on_every_surface(theme):
    table = _themes()[theme]
    failures = []
    for fg in FOREGROUNDS:
        for bg in BACKGROUNDS:
            value = contrast(_resolve(fg, table), _resolve(bg, table))
            if value < AA_BODY_TEXT:
                failures.append(f"{fg} on {bg} = {value:.2f}:1")
    assert not failures, (
        f"{theme} theme: these text/background pairs fall below WCAG AA ({AA_BODY_TEXT:.1f}:1): {failures}"
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_hovering_a_link_never_reduces_its_contrast(theme):
    """In light mode the accent darkens on hover, in dark mode it lightens.

    Before the accent tokens existed the hover step went the wrong way on
    dark: 2.05:1 dropping to 1.52:1.
    """
    table = _themes()[theme]
    surface = _resolve("--color-surface", table)
    rest = contrast(_resolve("--color-accent", table), surface)
    hover = contrast(_resolve("--color-accent-strong", table), surface)
    assert hover >= rest, (
        f"{theme} theme: hover contrast {hover:.2f}:1 is below resting {rest:.2f}:1"
    )


# --- UI quality programme, Phase 0 -------------------------------------------
# Supporting evidence only: these check token pairs as declared. Rendered
# components are measured in the browser (tools/qa/ui_scan.js).

NON_TEXT_MIN = 3.0  # WCAG 1.4.11: focus indicators and control boundaries


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(
    "fill", ["--color-amber-300", "--color-amber-400", "--color-amber-500"]
)
def test_text_on_amber_fills_is_readable(theme, fill):
    """The primary button is amber in both themes.

    Its text used --color-ink, which flips to near-white in the dark theme:
    1.77:1 on amber-400 (measured on /dashboard/, 2026-10-25). The fixed
    --color-on-amber keeps it dark everywhere.
    """
    table = _themes()[theme]
    value = contrast(_resolve("--color-on-amber", table), _resolve(fill, table))
    assert value >= AA_BODY_TEXT, f"{theme}: on-amber on {fill} = {value:.2f}:1"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_focus_ring_is_visible_on_every_surface(theme):
    """brand-600 measured 2.27:1 on the dark canvas before the token existed."""
    table = _themes()[theme]
    ring = _resolve("--color-focus-ring", table)
    failures = [
        f"{bg} = {contrast(ring, _resolve(bg, table)):.2f}:1"
        for bg in BACKGROUNDS
        if contrast(ring, _resolve(bg, table)) < NON_TEXT_MIN
    ]
    assert not failures, f"{theme}: focus ring below {NON_TEXT_MIN}:1 on {failures}"


TONE_FOREGROUNDS = [
    "--tone-brand-fg",
    "--tone-amber-fg",
    "--tone-teal-fg",
    "--tone-neutral-fg",
    "--tone-info-fg",
    "--tone-coral-fg",
    # --tone-purple-fg is not here: it points at Tailwind's built-in purple ramp
    # (OKLCH, not declared in app.css), so it cannot be resolved to a hex. It is
    # only the webhook-node accent on the automation canvas; the rendered scan
    # (tools/qa/ui_scan.js) measures it there.
]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_status_tone_text_meets_aa_on_surfaces(theme):
    """Field errors, danger buttons, deltas and badges use the tone foregrounds."""
    table = _themes()[theme]
    failures = []
    for fg in TONE_FOREGROUNDS:
        for bg in BACKGROUNDS:
            value = contrast(_resolve(fg, table), _resolve(bg, table))
            if value < AA_BODY_TEXT:
                failures.append(f"{fg} on {bg} = {value:.2f}:1")
    assert not failures, f"{theme}: {failures}"

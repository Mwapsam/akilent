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
from pathlib import Path

import pytest
from django.conf import settings

CSS = (Path(settings.BASE_DIR) / "assets" / "app.css").read_text(encoding="utf-8")
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
    light = _declarations(CSS[:CSS.index("@media (prefers-color-scheme: dark)")])
    dark_block = CSS[dark_start:]
    dark_block = dark_block[: dark_block.index("\n}")]
    dark = dict(light)
    dark.update(_declarations(dark_block))
    return {"light": light, "dark": dark}


def _resolve(name, table, seen=None):
    """Follow var() indirection to a literal hex."""
    seen = seen or set()
    assert name not in seen, "circular token reference at %s" % name
    seen.add(name)
    value = table[name]
    ref = re.fullmatch(r"var\((--[\w-]+)\)", value)
    if ref:
        return _resolve(ref.group(1), table, seen)
    assert re.fullmatch(r"#[0-9A-Fa-f]{6}", value), "%s is not a plain hex: %s" % (name, value)
    return value


def _channel(c):
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour):
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(fg, bg):
    a, b = _luminance(fg), _luminance(bg)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def test_contrast_helper_matches_known_values():
    """Guard the maths itself, so a broken helper cannot pass everything."""
    assert round(contrast("#000000", "#ffffff"), 2) == 21.0
    assert round(contrast("#ffffff", "#ffffff"), 2) == 1.0


FOREGROUNDS = ["--color-ink", "--color-ink-muted", "--color-ink-subtle",
               "--color-accent", "--color-accent-strong"]
BACKGROUNDS = ["--color-surface", "--color-canvas", "--color-surface-muted"]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_tokens_meet_aa_on_every_surface(theme):
    table = _themes()[theme]
    failures = []
    for fg in FOREGROUNDS:
        for bg in BACKGROUNDS:
            value = contrast(_resolve(fg, table), _resolve(bg, table))
            if value < AA_BODY_TEXT:
                failures.append("%s on %s = %.2f:1" % (fg, bg, value))
    assert not failures, (
        "%s theme: these text/background pairs fall below WCAG AA (%.1f:1): %s"
        % (theme, AA_BODY_TEXT, failures)
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
        "%s theme: hover contrast %.2f:1 is below resting %.2f:1"
        % (theme, hover, rest)
    )

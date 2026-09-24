"""Tinted component surfaces must use tone tokens, not light-only ramp steps.

The colour ramps (`brand-50`, `amber-100`, `gray-100` ...) hold the *same*
value in both themes - only the semantic tokens flip. So a component that fills
its background straight from a low ramp step renders as a bright patch on the
near-black dark canvas. This was measured, not guessed: `.badge-neutral` sat at
luminance 245 on a canvas of 21, and the same applied to `.nav-link-active`,
every `.alert-*`, and the dashboard work tiles.

The fix is the `--tone-*-bg/-border/-fg` tokens, which `color-mix` the accent
against `--color-surface` and therefore follow the theme.

Solid fills at -400/-500 and above are fine: those are deliberate brand colours
that stay put in both themes (the amber primary button, status dots).
"""

import re
from pathlib import Path

from django.conf import settings

CSS = Path(settings.BASE_DIR) / "assets" / "app.css"

# A background fed from a low (light-only) ramp step, inside a component rule.
LIGHT_RAMP_BG = re.compile(
    r"(?:\b|:)bg-(?:brand|amber|teal|coral|green|red|blue|gray)-(?:50|100|200)\b"
)


def _component_layer():
    source = CSS.read_text(encoding="utf-8")
    return source[source.index("@layer components") :]


def test_no_component_fills_from_a_light_only_ramp_step():
    offenders = []
    for rule in re.finditer(r"(\.[^{}\n]+)\{([^}]*)\}", _component_layer()):
        selector, body = rule.group(1).strip(), rule.group(2)
        if LIGHT_RAMP_BG.search(body):
            hit = LIGHT_RAMP_BG.search(body).group(0)
            offenders.append(f"{selector} -> {hit}")
    assert not offenders, (
        "These fill a background from a ramp step that is light in both themes, "
        "so they show as a bright patch in dark mode. Use the matching "
        f"--tone-*-bg token instead: {offenders}"
    )


def test_tone_tokens_are_defined_and_overridden_for_dark():
    source = CSS.read_text(encoding="utf-8")
    families = ["brand", "amber", "teal", "coral", "neutral", "info"]
    for family in families:
        assert f"--tone-{family}-bg:" in source, f"--tone-{family}-bg is not defined"
        assert f"--tone-{family}-fg:" in source, f"--tone-{family}-fg is not defined"
    # Both dark selectors must lift the foregrounds off the tint.
    assert source.count("--tone-brand-fg: var(--color-brand-200)") == 2, (
        "the dark overrides should appear once for prefers-color-scheme and "
        "once for [data-theme=dark]"
    )


# --- The same rule, applied to templates ---------------------------------

TEMPLATES = Path(settings.BASE_DIR) / "templates"

# Deliberately exempt. The marketing and docs shells are their own always-light
# design with a self-contained palette; example-embed.html is a plain HTML
# sample with no Django tags at all.
EXEMPT = (
    "docs/",
    "help/",
    "accounts/landing.html",
    "example-embed.html",
)

# Fills and borders from a ramp step that is light in both themes. Mid steps
# (-400 and up) are deliberate brand colour and stay.
LIGHT_RAMP_IN_TEMPLATE = re.compile(
    r"\b(?:bg|border|border-[lrtxy])-"
    r"(?:brand|amber|yellow|teal|green|emerald|coral|red|rose|blue|indigo|purple|gray|slate|zinc|neutral)-"
    r"(?:50|100|200)(?![0-9])"
)


def _app_templates():
    for path in sorted(TEMPLATES.rglob("*.html")):
        rel = path.relative_to(TEMPLATES).as_posix()
        if not any(rel.startswith(e) or rel == e for e in EXEMPT):
            yield rel, path


def test_no_template_tints_from_a_light_only_ramp_step():
    """Templates were the larger half of this defect.

    The component layer was converted first, but ~100 sites lived in template
    utility stacks - error callouts re-implementing .alert-danger, icon tiles,
    Alpine :class bindings and Django inline {% if %}s. Those are invisible to
    the component-layer check above, so they get their own.

    Use the tone-* utilities (tone-amber, tone-neutral-bg, tone-brand-border)
    or an existing component class (.alert-danger, .badge-*) instead.
    """
    offenders = []
    for rel, path in _app_templates():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = LIGHT_RAMP_IN_TEMPLATE.search(line)
            if match:
                offenders.append(f"{rel}:{number} -> {match.group(0)}")
    assert not offenders, (
        "These tint a surface from a ramp step that is light in both themes, so "
        "they render as a bright patch in dark mode: " + str(offenders)
    )

"""The compiled stylesheet must not contain platform-dependent float literals.

An arbitrary hex colour combined with an opacity modifier - `bg-[#FFB020]/40` -
makes Tailwind precompute an `oklab(81.3099% .0425804 .159407/.4)` literal. The
Windows and Linux builds of the standalone CLI round that last digit
differently, so the committed CSS can never match a CI rebuild and the build
guard fails forever on a difference that changes nothing visually.

Using the theme token instead (`bg-amber-400/40`) emits `#ffb02066`, which is
identical everywhere. docs/design/akilent-ui-spec.md §17 wants tokens here
anyway, so this test enforces a rule the spec already states.
"""

import re
from pathlib import Path

from django.conf import settings

COMPILED_CSS = Path(settings.BASE_DIR) / "static" / "css" / "app.css"
TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"

# oklab(<number> ... - a precomputed literal. "color-mix(in oklab, ...)" is fine:
# it defers the maths to the browser and carries no float in the source.
PRECOMPUTED_OKLAB = re.compile(r"oklab\(\s*[0-9.]")

# An arbitrary hex colour with an opacity modifier, e.g. text-[#1FAF9C]/80
ARBITRARY_HEX_WITH_OPACITY = re.compile(
    r"\b(?:text|bg|border|ring|from|to|via|fill|stroke|decoration|outline|shadow)"
    r"-\[#[0-9A-Fa-f]{3,8}\]/[0-9]+"
)


def test_compiled_css_has_no_precomputed_oklab_literals():
    css = COMPILED_CSS.read_text(encoding="utf-8")
    hits = PRECOMPUTED_OKLAB.findall(css)
    assert not hits, (
        f"{len(hits)} precomputed oklab() literal(s) in static/css/app.css. "
        "These differ between the Windows and Linux Tailwind builds and will "
        "break the CI build guard. Replace the arbitrary hex colour that "
        "produced them with the equivalent theme token."
    )


def test_no_template_uses_an_arbitrary_hex_with_opacity():
    offenders = {}
    for path in TEMPLATES_DIR.rglob("*.html"):
        found = ARBITRARY_HEX_WITH_OPACITY.findall(path.read_text(encoding="utf-8"))
        if found:
            offenders[str(path.relative_to(TEMPLATES_DIR)).replace("\\", "/")] = found
    assert not offenders, (
        "Arbitrary hex colours with an opacity modifier compile to a "
        "platform-dependent oklab() literal. Use the theme token instead "
        f"(e.g. bg-amber-400/40): {offenders}"
    )

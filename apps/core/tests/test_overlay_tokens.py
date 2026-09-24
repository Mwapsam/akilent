"""Overlays in the app shell must use the z-index token scale.

docs/design/akilent-ui-spec.md §17 requires that modal/drawer code carry no
ad-hoc `z-40` / `z-50` / `bg-black/40`. Those Tailwind utilities sit far below
the token scale (`--z-dropdown: 1000` ... `--z-toast: 1080`), so a bespoke
drawer at `z-50` renders *underneath* anything using the tokens - which is how
the conversation drawer ended up below real modals.

Scope is templates that extend base.html. accounts/landing.html and
docs/_layout.html are standalone public shells with their own token blocks and
no app overlays; merging those is separate work.
"""

import re
from pathlib import Path

from django.conf import settings

TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"
AD_HOC = re.compile(r"bg-black/[0-9]+|(?<![\w-])z-(?:40|50)(?![\w-])")


def _app_templates():
    """Templates rendered inside the app shell."""
    for path in TEMPLATES_DIR.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        if 'extends "base.html"' not in source:
            continue
        yield str(path.relative_to(TEMPLATES_DIR)).replace("\\", "/"), source


def test_app_overlays_use_the_z_index_tokens():
    offenders = {
        rel: sorted(set(found))
        for rel, source in _app_templates()
        if (found := AD_HOC.findall(source))
    }
    assert not offenders, (
        "Use the z-index tokens (style=\"z-index: var(--z-drawer)\" / --z-modal) "
        "and bg-gray-900/50 instead of ad-hoc z-40 / z-50 / bg-black: "
        f"{offenders}"
    )

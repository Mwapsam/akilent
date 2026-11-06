"""Every icon name a template asks for must exist in apps/core/icons.py.

components/icon.html looks the name up there (``{% icon_paths name %}``). A name it
doesn't know renders an empty <svg> - the icon is simply invisible, with no error
anywhere. Five such names had accumulated (credit-card, heartbeat, alert-circle,
alert-triangle, trash-2), two of them in the admin sidebar.
"""

import re
from pathlib import Path

from django.conf import settings

TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"

# `{% include "components/icon.html" with name="x" %}`
INCLUDE_RE = re.compile(r'icon\.html"\s+with\s+name="([a-z0-9-]+)"')
# `icon="x"` passed to a partial that forwards it to icon.html
ARG_RE = re.compile(r'\bicon="([a-z0-9-]+)"')


def _defined_names():
    from apps.core.icons import ICONS

    return set(ICONS)


def _referenced_names():
    found = {}
    for path in TEMPLATES_DIR.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        for name in set(INCLUDE_RE.findall(source)) | set(ARG_RE.findall(source)):
            found.setdefault(name, set()).add(
                str(path.relative_to(TEMPLATES_DIR)).replace("\\", "/")
            )
    return found


def test_every_referenced_icon_is_defined():
    defined = _defined_names()
    missing = {
        name: sorted(files)
        for name, files in _referenced_names().items()
        if name not in defined
    }
    assert not missing, (
        "Icon names used but not defined in apps/core/icons.py: "
        + "; ".join(
            f"{name} ({', '.join(files)})" for name, files in sorted(missing.items())
        )
    )


def test_icon_template_defines_a_reasonable_set():
    assert len(_defined_names()) > 40


def test_an_icon_renders_its_paths_and_an_unknown_one_renders_empty():
    from django.template.loader import render_to_string

    from apps.core.icons import ICONS

    check = render_to_string(
        "components/icon.html", {"name": "check", "class": "w-4 h-4"}
    )
    assert ICONS["check"] in check and 'class="w-4 h-4"' in check
    unknown = render_to_string("components/icon.html", {"name": "no-such-icon"})
    assert "<path" not in unknown and "<svg" in unknown

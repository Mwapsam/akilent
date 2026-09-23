"""Every icon name a template asks for must exist in components/icon.html.

components/icon.html is an {% if %}/{% elif %} chain on `name`. A name with no
branch falls through and renders an empty <svg> - the icon is simply invisible,
with no error anywhere. Five such names had accumulated (credit-card, heartbeat,
alert-circle, alert-triangle, trash-2), two of them in the admin sidebar.
"""

import re
from pathlib import Path

from django.conf import settings

TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"
ICON_TEMPLATE = TEMPLATES_DIR / "components" / "icon.html"

# `{% include "components/icon.html" with name="x" %}`
INCLUDE_RE = re.compile(r'icon\.html"\s+with\s+name="([a-z0-9-]+)"')
# `icon="x"` passed to a partial that forwards it to icon.html
ARG_RE = re.compile(r'\bicon="([a-z0-9-]+)"')
DEFINED_RE = re.compile(r'name == "([a-z0-9-]+)"')


def _defined_names():
    return set(DEFINED_RE.findall(ICON_TEMPLATE.read_text(encoding="utf-8")))


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
    assert not missing, "Icon names used but not defined in components/icon.html: " + "; ".join(
        f"{name} ({', '.join(files)})" for name, files in sorted(missing.items())
    )


def test_icon_template_defines_a_reasonable_set():
    # Guards against the regex silently matching nothing if the chain is rewritten.
    assert len(_defined_names()) > 40

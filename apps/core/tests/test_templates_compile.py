"""Every template under templates/ parses.

A page that no test renders can carry a broken block (a stray {% endfor %} after an
edit) all the way to production, where it is a 500 for whoever opens it. Compiling
each one is cheap and catches that here.
"""

from pathlib import Path

import pytest
from django.conf import settings
from django.template.loader import get_template

ROOT = Path(settings.BASE_DIR) / "templates"
NAMES = sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*.html"))


@pytest.mark.parametrize("name", NAMES)
def test_template_compiles(name):
    get_template(name)

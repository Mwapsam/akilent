"""The app's stylesheet source as one text, for tests that read CSS.

The design tokens (@theme, the font face, the dark-theme overrides) live in
assets/tokens.css, which both assets/app.css and assets/landing.css import. A test
asking "is this token defined, and to what?" reads both files together.
"""

from pathlib import Path

from django.conf import settings


def app_css_source() -> str:
    assets = Path(settings.BASE_DIR) / "assets"
    return "\n".join(
        (assets / name).read_text(encoding="utf-8")
        for name in ("tokens.css", "app.css")
    )

"""Template comment hygiene.

Two rules, both of which have bitten this codebase:

1. No ``<!-- -->`` in a Django template. An HTML comment is sent to the browser,
   so internal notes ship to every visitor and cost bytes on every render.

2. No *multi-line* ``{# ... #}``. This is the dangerous one: Django's ``{# #}``
   comment only works on a single line. Spread it over two and it stops being a
   comment entirely - the text is rendered into the page, in full, to every
   visitor. It fails silently, with no error anywhere. Use
   ``{% comment %}...{% endcomment %}`` for anything spanning more than one line.

templates/example-embed.html is exempt - it contains no Django tags at all. It
is a plain static HTML sample, so Django comment syntax there would render as
literal text.
"""

import re
from pathlib import Path

from django.conf import settings

TEMPLATES_DIR = Path(settings.BASE_DIR) / "templates"
HTML_COMMENT = re.compile(r"<!--(?!\[if ).*?-->", re.S)  # keep IE conditionals
DJANGO_COMMENT = re.compile(r"\{#.*?#\}", re.S)
EXEMPT = {"example-embed.html"}


def _django_templates():
    for path in TEMPLATES_DIR.rglob("*.html"):
        rel = str(path.relative_to(TEMPLATES_DIR)).replace("\\", "/")
        if rel in EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        if "{%" not in source and "{#" not in source:
            continue  # not a Django template
        yield rel, source


def test_no_html_comments_in_django_templates():
    offenders = {
        rel: [c.strip()[:60] for c in found]
        for rel, source in _django_templates()
        if (found := HTML_COMMENT.findall(source))
    }
    assert not offenders, (
        "Use {% comment %}...{% endcomment %} instead of an HTML comment - "
        f"HTML comments are served to the browser: {offenders}"
    )


def test_no_multiline_hash_comments():
    """A {# #} spanning two lines is not a comment - it renders to the page."""
    offenders = {}
    for rel, source in _django_templates():
        for match in DJANGO_COMMENT.finditer(source):
            if "\n" in match.group(0):
                line = source[: match.start()].count("\n") + 1
                offenders.setdefault(rel, []).append(
                    f"line {line}: {match.group(0)[:50].splitlines()[0]}..."
                )
    assert not offenders, (
        "{# #} only comments a single line. These span several, so Django "
        "renders them into the page verbatim. Use "
        f"{{% comment %}}...{{% endcomment %}}: {offenders}"
    )

"""Sandboxed rendering of EmailTemplate content.

Uses a plain django.template.Engine instance (not the app-configured one) so
tenant-authored template content only ever sees the variables dict passed at
render time — never settings, the request, or app models. Never render with
a RequestContext here.
"""
from __future__ import annotations

import re

from django.template import Context, Engine

_VARIABLE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_.]*)")

_ENGINE = Engine(
    debug=False,
    libraries={},
    builtins=[
        "django.template.defaulttags",
        "django.template.defaultfilters",
        "apps.email.template_components",  # {% component "AkilentButton" ... %}
    ],
)


def render_string(source: str, variables: dict | None = None) -> str:
    """Render a single Django-template-syntax string with `variables`."""
    if not source:
        return source
    return _ENGINE.from_string(source).render(Context(variables or {}, autoescape=True))


def _account_components(template) -> dict:
    account_id = getattr(template, "account_id", None)
    if not account_id:
        return {}
    from apps.email.models import TemplateComponent

    return {
        c.name: c.html
        for c in TemplateComponent.objects.filter(account_id=account_id)
    }


def render_template(template, variables: dict | None = None) -> tuple[str, str, str]:
    """Render an EmailTemplate's subject/text/html with `variables`.

    Returns (subject, text_body, html_body). The tenant's custom
    ``{% component %}`` bodies are made available via a ``_components`` key.
    """
    variables = {**(variables or {})}
    variables.setdefault("_components", _account_components(template))
    subject = render_string(template.subject, variables)
    text_body = render_string(template.text_body, variables)
    html_body = render_string(template.html_body, variables)
    return subject, text_body, html_body


def find_variable_paths(source: str) -> set[str]:
    """Return the full dotted `{{ a.b.c }}` paths referenced in `source`.

    A plain regex scan over tag names, not a template parse — safe to run on
    unsanitized/untrusted source since nothing is executed.
    """
    if not source:
        return set()
    return set(_VARIABLE_RE.findall(source))


def find_variables(source: str) -> set[str]:
    """Return the top-level `{{ name }}` variable names referenced in `source`
    (the part before the first `.` in any dotted path)."""
    return {path.split(".")[0] for path in find_variable_paths(source)}


def _resolve_path(variables: dict, path: str) -> bool:
    """Return whether dotted `path` (e.g. "contact.first_name") resolves to a
    value by walking nested dicts in `variables`."""
    current = variables
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def flatten_variable_paths(variables: dict, prefix: str = "") -> list[str]:
    """Flatten a (possibly nested) variables dict into dotted leaf paths,
    e.g. {"first_name": "Ada", "contact": {"phone": "..."}} -> ["first_name",
    "contact.phone"]. Used to populate merge-tag palettes/autocomplete."""
    paths: list[str] = []
    for key, value in variables.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            paths.extend(flatten_variable_paths(value, path))
        else:
            paths.append(path)
    return sorted(paths)


def validate_variables(template, variables: dict | None = None) -> list[str]:
    """Return the sorted list of dotted variable paths referenced by
    `template` that don't resolve against `variables` (nested dicts are
    walked, so `{{ contact.first_name }}` is only satisfied by a
    `{"contact": {"first_name": ...}}` shape, not merely a `contact` key)."""
    variables = variables or {}
    referenced: set[str] = set()
    for source in (template.subject, template.text_body, template.html_body):
        referenced |= find_variable_paths(source)
    return sorted(path for path in referenced if not _resolve_path(variables, path))

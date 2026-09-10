"""The ``{% component %}`` tag for the sandboxed email template engine.

Registered as a builtin on the render engine (``apps.email.services.render``),
so templates use it without ``{% load %}``. Custom per-tenant components are
resolved from a ``_components`` dict injected into the render context by
``render_template``; built-ins below need no DB row.

    {% component "AkilentButton" href="https://acme.com/pay" label="Pay now" %}

Component bodies are themselves Django-template strings rendered with the
tag's kwargs as their entire context (no access to the outer context or any
variable dict) — one level deep, no nested ``{% component %}``.
"""
from __future__ import annotations

from django.template import Context, Engine, Library
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = Library()

# Minimal, email-client-safe inline-styled defaults.
BUILTINS: dict[str, str] = {
    "AkilentHeader": (
        '<tr><td style="padding:24px 0;text-align:center;font:600 20px system-ui,sans-serif;'
        'color:#111">{{ title|default:"" }}</td></tr>'
    ),
    "AkilentButton": (
        '<tr><td style="padding:16px 0;text-align:center">'
        '<a href="{{ href }}" style="display:inline-block;padding:12px 24px;border-radius:8px;'
        'background:#4f46e5;color:#fff;font:600 14px system-ui,sans-serif;text-decoration:none">'
        '{{ label|default:"Open" }}</a></td></tr>'
    ),
    "AkilentInvoice": (
        '<tr><td style="padding:16px 0;font:14px system-ui,sans-serif;color:#111">'
        'Invoice <strong>{{ number }}</strong> — '
        '<strong>{{ currency|default:"" }}{{ amount }}</strong>'
        '{% if due %} · due {{ due }}{% endif %}</td></tr>'
    ),
    "AkilentFooter": (
        '<tr><td style="padding:24px 0;text-align:center;font:12px system-ui,sans-serif;'
        'color:#888">{{ text|default:"" }}</td></tr>'
    ),
}

_COMPONENT_ENGINE = Engine(
    debug=False,
    libraries={},
    builtins=["django.template.defaulttags", "django.template.defaultfilters"],
)


class UnknownComponentError(Exception):
    pass


@register.simple_tag(takes_context=True)
def component(context, name, **kwargs):
    body = BUILTINS.get(name)
    if body is None:
        custom = (context.get("_components") or {})
        body = custom.get(name)
    if body is None:
        raise UnknownComponentError(f"unknown component {name!r}")
    # kwargs are untrusted caller input. The template parser marks `{% %}`
    # string literals safe, so escape unconditionally (str() drops any existing
    # safe marker) before the value reaches the component body.
    safe_kwargs = {
        k: (escape(str(v)) if isinstance(v, str) else v)
        for k, v in kwargs.items()
    }
    rendered = _COMPONENT_ENGINE.from_string(body).render(
        Context(safe_kwargs, autoescape=True)
    )
    return mark_safe(rendered)

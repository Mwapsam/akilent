"""Template helpers for the primary navigation.

Replaces the scattered ``{% if '/foo' in request.path %}`` checks in
``templates/components/_nav.html`` with one tested helper.
"""

from __future__ import annotations

from django import template
from django.urls import NoReverseMatch, reverse

register = template.Library()


def _current_path(context) -> str:
    request = context.get("request")
    return getattr(request, "path", "") or ""


def _matches(path: str, target: str, *, exact: bool) -> bool:
    if not path or not target:
        return False
    if target != "/":
        target = "/" + target.strip("/") + "/"
    if exact:
        return path == target
    return path == target or path.startswith(target)


@register.simple_tag(takes_context=True)
def nav_active(context, *targets, exact=False, css="nav-link-active"):
    """Return ``css`` when the current path matches any of ``targets``.

    Each target is either a URL path fragment (``"email/campaigns"``,
    ``"/email/campaigns/"``) or a resolvable URL name (``"dashboard"``,
    ``"logs:messages"``); names that fail to reverse are treated as path
    fragments so callers can pass either.  Matching is prefix-based unless
    ``exact=True`` so a detail page keeps its section highlighted.
    """
    path = _current_path(context)
    # Allow a single space-separated string so {% include %}-based partials can
    # pass multiple targets through one argument.
    if len(targets) == 1 and isinstance(targets[0], str) and " " in targets[0]:
        targets = tuple(targets[0].split())
    for target in targets:
        candidate = target
        if "/" not in target:
            try:
                candidate = reverse(target)
            except NoReverseMatch:
                candidate = target
        if _matches(path, candidate, exact=exact):
            return css
    return ""


@register.simple_tag(takes_context=True)
def is_nav_active(context, *targets, exact=False):
    """Boolean form of :func:`nav_active` — handy for ``aria-current``."""
    return bool(nav_active(context, *targets, exact=exact))

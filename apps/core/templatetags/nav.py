"""Template helpers for the primary navigation.

Replaces the scattered ``{% if '/foo' in request.path %}`` checks in
``templates/components/_nav.html`` with one tested helper.
"""

from __future__ import annotations

from django import template
from django.urls import NoReverseMatch, reverse

register = template.Library()


def _split(targets) -> tuple:
    # Allow a single space-separated string so {% include %}-based partials can
    # pass multiple targets through one argument.
    if len(targets) == 1 and isinstance(targets[0], str) and " " in targets[0]:
        return tuple(targets[0].split())
    return targets


def _resolve(target: str) -> str:
    if "/" not in target:
        try:
            return reverse(target)
        except NoReverseMatch:
            return target
    return target


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
def nav_active(context, *targets, exact=False, exclude="", css="nav-link-active"):
    """Return ``css`` when the current path matches any of ``targets``.

    Each target is either a URL path fragment (``"email/campaigns"``,
    ``"/email/campaigns/"``) or a resolvable URL name (``"dashboard"``,
    ``"logs:messages"``); names that fail to reverse are treated as path
    fragments so callers can pass either.  Matching is prefix-based unless
    ``exact=True`` so a detail page keeps its section highlighted.

    ``exclude`` (space-separated, same forms as targets) carves sub-sections that
    have their own nav link out of a prefix match: Inbox owns ``/inbox/`` but not
    ``/inbox/followups/``, which is Follow-ups' page. Without it both links were
    marked current there.
    """
    path = _current_path(context)
    if exclude and any(
        _matches(path, _resolve(t), exact=False) for t in exclude.split()
    ):
        return ""
    for target in _split(targets):
        if _matches(path, _resolve(target), exact=exact):
            return css
    return ""


@register.simple_tag(takes_context=True)
def is_nav_active(context, *targets, exact=False, exclude=""):
    """Boolean form of :func:`nav_active` — handy for ``aria-current``."""
    return bool(nav_active(context, *targets, exact=exact, exclude=exclude))


@register.simple_tag
def nav_targets(*targets):
    """The paths a nav link counts as its own, for ``data-nav-match``.

    Back/forward restores only ``<main>``, so static/js/app.js re-marks the active link in the
    browser with the same prefix rule as :func:`nav_active`.
    """
    paths = []
    for target in _split(targets):
        candidate = _resolve(target)
        if candidate != "/":
            candidate = "/" + candidate.strip("/") + "/"
        paths.append(candidate)
    return " ".join(paths)

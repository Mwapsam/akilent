"""Guards for the HTMX migration of the old hand-rolled ``data-ajax`` engine.

These are contract tests about the markup, not about any one screen. They exist
because the migration is the kind that half-lands: a form keeps working in the
browser while quietly losing its no-JavaScript fallback, and nothing fails.
"""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[3] / "templates"
FORM_TAG = re.compile(r"<form\b[^>]*>", re.S)


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def test_the_retired_data_ajax_engine_has_no_callers_left():
    """``data-ajax`` and friends are gone from static/js/app.js, so a template
    still using them would silently do a full page POST instead of swapping."""
    offenders = [
        str(p.relative_to(TEMPLATES))
        for p in _templates()
        if re.search(r"\bdata-(ajax|swap|append|remove|reset)\b", p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "these templates still use the removed data-ajax engine: %s" % offenders
    )


def test_every_hx_post_form_still_works_without_javascript():
    """Progressive enhancement: HTMX is an enhancement over a real form post.

    A ``<form hx-post>`` that dropped its own ``action``/``method`` would do
    nothing at all with JavaScript off, which is a regression the browser will
    not report.
    """
    offenders = []
    for p in _templates():
        for tag in FORM_TAG.findall(p.read_text(encoding="utf-8")):
            if "hx-post=" not in tag:
                continue
            if 'method="post"' not in tag.lower() or "action=" not in tag:
                offenders.append("%s: %s" % (p.relative_to(TEMPLATES), tag[:90]))
    assert not offenders, (
        "hx-post forms missing a plain method/action fallback: %s" % offenders
    )


def test_hx_post_targets_the_same_url_the_plain_form_posts_to():
    """The two paths must not drift apart — a form that posts one URL normally
    and a different one under HTMX is a bug that only shows up with JS on."""
    offenders = []
    for p in _templates():
        for tag in FORM_TAG.findall(p.read_text(encoding="utf-8")):
            hx = re.search(r'hx-post="([^"]*)"', tag)
            action = re.search(r'action="([^"]*)"', tag)
            if hx and action and hx.group(1) != action.group(1):
                offenders.append(
                    "%s: action=%r hx-post=%r"
                    % (p.relative_to(TEMPLATES), action.group(1), hx.group(1))
                )
    assert not offenders, "hx-post disagrees with action: %s" % offenders


@pytest.mark.parametrize(
    "attribute",
    ["hx-confirm-danger", "hx-confirm-label"],
)
def test_confirm_modifiers_only_appear_beside_hx_confirm(attribute):
    """Both are read by the htmx:confirm bridge in static/js/app.js, which only
    runs when hx-confirm supplied a question. Alone they do nothing."""
    offenders = []
    for p in _templates():
        for tag in FORM_TAG.findall(p.read_text(encoding="utf-8")):
            if attribute in tag and "hx-confirm=" not in tag:
                offenders.append("%s: %s" % (p.relative_to(TEMPLATES), tag[:90]))
    assert not offenders, "%s without hx-confirm: %s" % (attribute, offenders)

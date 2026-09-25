"""The z-index scale, and the two rules that make it work.

Both bugs this pins were reported from the running app and neither shows up in
a template test, because nothing here is wrong in isolation — the ordering only
breaks when two correct-looking numbers meet on the same screen.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
CSS = (ROOT / "assets" / "app.css").read_text(encoding="utf-8")
TEMPLATES = ROOT / "templates"


def _layer(name):
    m = re.search(r"--z-%s:\s*(\d+)" % re.escape(name), CSS)
    assert m, "no --z-%s in the token layer" % name
    return int(m.group(1))


def test_a_dropdown_opens_above_the_sticky_topbar_and_the_onboarding_widget():
    """The reported bug. Our dropdowns are not all inside the sticky bar —
    help tips, {% dropdown %} menus and the conversation composer's saved-reply
    popover open from page content, in the root stacking context. Below
    --z-sticky they open *behind* the topbar, and behind the "Finish setup"
    widget that is pinned to the same bottom-right corner the composer popover
    expands into.
    """
    assert _layer("dropdown") > _layer("sticky")


def test_the_scale_still_stacks_in_the_right_order_overall():
    order = ["sticky", "dropdown", "drawer", "modal", "toast"]
    values = [_layer(n) for n in order]
    assert values == sorted(values), dict(zip(order, values))


def test_a_drawer_covers_a_dropdown():
    """A drawer is an overlay and owns the screen while it is open; a dropdown
    is not, and must not outrank it."""
    assert _layer("drawer") > _layer("dropdown")


@pytest.mark.parametrize(
    "template",
    ["base.html", "components/_dropdown.html"],
)
def test_dropdowns_do_not_lock_body_scroll(template):
    """The reported "UI shake".

    Alpine's `.noscroll` sets overflow:hidden on <body>. On any page tall
    enough to have a scrollbar that removes it, the content reflows by the
    scrollbar's width, and the whole page visibly jumps each time the menu
    opens or closes. Scroll-locking behind a small menu is modal behaviour; a
    dropdown should leave the page alone.
    """
    source = (TEMPLATES / template).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "x-trap.noscroll" in line and "drawerOpen" not in line
    ]
    assert not offenders, (
        "%s locks body scroll for a dropdown, which shifts the layout: %s"
        % (template, offenders)
    )


def test_the_drawer_does_still_lock_scroll():
    """Guards the exemption above — the drawer is a real overlay and should
    keep the page still underneath it."""
    source = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "x-trap.noscroll=\"$store.ui.drawerOpen\"" in source

"""A smoke test for the accessibility contract the app shell claims to keep.

There was no automated front-end or a11y check of any kind before this: all
the coverage was Django view tests, so every defect below was found by reading
markup rather than by anything failing. These pin the fixes so they cannot
regress silently.

This is a markup contract, not a substitute for axe or a real screen reader -
it checks the specific things that were wrong, not WCAG as a whole.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership

TEMPLATES = Path(settings.BASE_DIR) / "templates"
BASE = (TEMPLATES / "base.html").read_text(encoding="utf-8")
NAV = (TEMPLATES / "components" / "_nav.html").read_text(encoding="utf-8")
NAV_ITEM = (TEMPLATES / "components" / "_nav_item.html").read_text(encoding="utf-8")
PALETTE = (TEMPLATES / "components" / "command_palette.html").read_text(encoding="utf-8")
TOAST = (TEMPLATES / "components" / "toast.html").read_text(encoding="utf-8")
APP_JS = (Path(settings.BASE_DIR) / "static" / "js" / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def member(db):
    user = User.objects.create_user("owner", "owner@example.com", "Sup3r-secret-pw")
    account = Account.objects.create(
        company_name="Acme",
        selected_services=Account.Services.EMAIL,
        onboarding_state=Account.Onboarding.ACCOUNT_CREATED,
        email_verified=True,
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user


# --- Rendered shell -------------------------------------------------------

@pytest.mark.django_db
def test_shell_renders_skip_link_and_landmarks(client, member):
    client.force_login(member)
    body = client.get("/dashboard/").content.decode()
    assert 'href="#main"' in body, "the skip link is the only way past the nav"
    assert 'id="main"' in body
    assert "<main" in body
    assert 'aria-label="Primary"' in body


@pytest.mark.django_db
def test_current_page_is_marked_for_assistive_tech(client, member):
    client.force_login(member)
    body = client.get("/dashboard/").content.decode()
    assert 'aria-current="page"' in body


# --- The user menu --------------------------------------------------------

def test_user_menu_trigger_has_an_accessible_name():
    """Its avatar initial and chevron are both aria-hidden.

    Without an explicit label the button announced as "button, collapsed" -
    no indication of what it opens.
    """
    trigger = BASE[BASE.index('aria-controls="user-menu-panel"') - 600:
                   BASE.index('aria-controls="user-menu-panel"') + 100]
    assert 'aria-label="Account menu"' in trigger


def test_user_menu_does_not_claim_unimplemented_menu_semantics():
    """role=menu commits to roving tabindex and arrow keys, which this panel
    never had - and it holds an aria-pressed theme toggle, which is not a
    valid menu child at all. It is a disclosure, so it says so.
    """
    assert 'role="menu"' not in BASE
    assert 'role="menuitem"' not in BASE
    assert 'aria-haspopup' not in BASE


# --- Sidebar arrow-key navigation ----------------------------------------

def test_arrow_key_nav_reaches_every_link_including_inbox_and_dashboard():
    """Inbox and Dashboard sit above the first [role=group].

    Scoping the handler to the closest group meant arrow keys did nothing on
    the product's two most-used links.
    """
    assert "link.closest('nav')" in APP_JS
    assert 'link.closest(\'[role="group"]\')' not in APP_JS


def test_no_dead_nav_group_contract():
    """data-nav-group was emitted on every link and read by nothing."""
    assert "data-nav-group" not in NAV_ITEM
    assert "data-nav-group" not in APP_JS
    assert "group_key" not in NAV


# --- Command palette ------------------------------------------------------

def test_command_palette_listbox_is_wired_to_its_input():
    """Focus never leaves the input, so without aria-activedescendant a
    screen reader announces nothing while arrowing through results.
    """
    assert 'role="combobox"' in PALETTE
    assert 'aria-controls="cmdk-results"' in PALETTE
    assert "aria-activedescendant" in PALETTE
    assert 'id="cmdk-results"' in PALETTE
    assert 'role="option"' in PALETTE
    assert "aria-selected" in PALETTE


def test_command_palette_options_are_not_nested_in_interactive_wrappers():
    """An option must not contain its own interactive element, so role=option
    belongs on the anchor and the li is presentational."""
    assert 'role="presentation"' in PALETTE


# --- Toasts ---------------------------------------------------------------

def test_toast_region_announces_once():
    """role=status inside aria-live nests two live regions, which reads each
    message twice; aria-atomic=true re-read every toast on each new one."""
    assert TOAST.count('aria-live="polite"') == 1
    assert 'role="status"' not in re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}",
                                         "", TOAST, flags=re.S)
    assert 'aria-atomic="false"' in TOAST

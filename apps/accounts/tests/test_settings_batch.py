"""The Settings-style batch (2026-11-09): Inbox settings, Content Studio AI and Bio page, and
Channels in the two-column sections, and the sticky "Unsaved changes" bar.

- every settings form keeps a plain Save for a page without scripts and gets the bar with them;
- the converted pages are .settings-section groups with no cards;
- Channels ends in a danger section for Disconnect.
"""

import pytest
from django.conf import settings
from django.contrib.auth.models import User

from apps.accounts.models import Account, Membership

TEMPLATES = settings.BASE_DIR / "templates"


def _src(path):
    return (TEMPLATES / path).read_text(encoding="utf-8")


@pytest.fixture
def owner_client(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client


# --- The "Unsaved changes" bar --------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/settings/", "/settings/business/", "/settings/hours/", "/settings/ai/"],
)
def test_settings_forms_get_the_bar_and_keep_a_plain_save(owner_client, path):
    page = owner_client.get(path).content.decode()
    form = page[page.index("data-save-bar") :]
    form = form[: form.index("</form>")]
    assert "data-save-fallback" in form
    assert 'class="save-bar" role="region" aria-label="Unsaved changes"' in form
    assert 'type="reset" class="btn btn-ghost btn-sm">Discard' in form


def test_the_bar_shows_only_with_scripts_and_once_a_field_changed():
    css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    assert ".save-bar { display: none; }" in css
    assert (
        "html.ak-js form[data-save-bar] [data-save-fallback] { display: none; }" in css
    )
    assert "html.ak-js form[data-save-bar][data-dirty] .save-bar {" in css
    js = (settings.BASE_DIR / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert 'document.documentElement.classList.add("ak-js")' in js
    assert '"beforeunload"' in js and "Leave without saving?" in js


# --- Two-column sections, no cards ----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "conversations/saved_replies.html",
        "conversations/inbox_settings_teams.html",
        "publishing/ai_tools.html",
        "publishing/bio.html",
        "publishing/channels.html",
    ],
)
def test_the_page_is_settings_sections_with_no_cards(path):
    src = _src(path)
    assert 'class="settings-section' in src
    assert "card card-pad" not in src and "cs-cols" not in src


def test_inbox_settings_render_as_sections_with_the_highlight_tab(owner_client):
    page = owner_client.get("/inbox/settings/teams/").content.decode()
    assert 'id="teams" class="settings-section' in page
    assert 'id="routing" class="settings-section' in page
    # Stage 5: adding a rule is a modal (with "New team…" in it), not a disclosure.
    assert '/inbox/routing-rules/?add=1" data-overlay-open="modal"' in page
    assert "border-brand-600" not in page
    replies = owner_client.get("/inbox/saved-replies/").content.decode()
    assert 'aria-labelledby="replies-title"' in replies
    assert "No saved replies yet." in replies


def test_studio_settings_forms_get_the_bar():
    for path in ("publishing/ai_tools.html", "publishing/bio.html"):
        src = _src(path)
        assert "data-save-bar" in src and "data-save-fallback" in src
        assert '{% include "components/_save_bar.html"' in src


def test_channels_ends_in_a_danger_section_for_disconnect():
    src = _src("publishing/channels.html")
    danger = src[src.index("settings-section--danger") :]
    assert "publishing:fb-disconnect" in danger and "btn-danger-outline" in danger
    assert src.count("publishing:fb-disconnect") == 1
    assert "publishing:fb-connect" in src  # connecting stays reachable here

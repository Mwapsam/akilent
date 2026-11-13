"""Panels step 4 (2026-11-09): the remaining Settings-style pages on panels. Inbox settings
(Saved replies, Teams & routing, a team), Content Studio's AI tools and Bio page, and Channels:
one 720px column of panels, Add at the panel's right, lists as hairline rows, Save in the
footer, Disconnect last in a danger panel."""

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from apps.accounts.models import Membership, Team
from apps.publishing import ai_plus, bio
from apps.publishing.tests.test_studio_overlay import shop  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db

# The Settings panels. No closing quote: the studio's own pages add cs-panels after it
# (full width, static/css/studio.css).
COLUMN = 'class="settings-main settings-main--panels'


def _main(html):
    return html[html.index(COLUMN) :]


def test_inbox_settings_pages_are_panels(shop):  # noqa: F811
    account, client = shop
    replies = _main(client.get(reverse("conversations:saved_replies")).content.decode())
    assert '<section class="panel" aria-labelledby="replies-title">' in replies
    assert '<div class="panel-actions"><a href="/inbox/saved-replies/?add=1"' in replies

    teams = _main(
        client.get(reverse("conversations:inbox_settings_teams")).content.decode()
    )
    assert 'class="panel scroll-mt-24" aria-labelledby="teams-title"' in teams
    assert 'class="panel scroll-mt-24" aria-labelledby="routing-title"' in teams
    assert "settings-section" not in teams


def test_a_team_is_panels_with_add_in_the_footer(shop):  # noqa: F811
    account, client = shop
    mate = User.objects.create_user("m", "m@example.com", "pw")
    Membership.objects.create(user=mate, account=account, role=Membership.Role.MEMBER)
    team = Team.objects.create(account=account, name="Sales")
    page = _main(
        client.get(
            reverse("conversations:team_detail", args=[team.pk])
        ).content.decode()
    )
    assert 'aria-labelledby="team-members-h"' in page and "table-wrap" not in page
    add = page[page.index('aria-labelledby="team-add-h"') :]
    assert add.index('<div class="panel-foot">') < add.index("Add to team")


def test_studio_ai_and_bio_are_panels_with_save_in_the_footer(shop, settings):  # noqa: F811
    account, client = shop
    settings.RELEASED_FEATURES = frozenset(
        {ai_plus.FLAG, bio.FLAG}
    )  # both tabs released
    ai = client.get(reverse("publishing:ai-tools")).content.decode()
    assert COLUMN in ai and "_save_bar" not in ai
    assert "data-panel-save" in ai or "Only an Owner or Admin can change it." in ai
    page = client.get(reverse("publishing:bio")).content.decode()
    assert COLUMN in page and 'class="btn btn-primary btn-sm">Create bio page' in page


def test_channels_is_panels_with_disconnect_last(shop):  # noqa: F811
    account, client = shop
    page = _main(client.get(reverse("publishing:channels")).content.decode())
    for heading in ("ch-ig", "ch-fb", "ch-review"):
        assert f'<section class="panel" aria-labelledby="{heading}">' in page
    # The status sits at the right of each account's header, as on Settings › Integrations.
    ig = page[
        page.index('aria-labelledby="ch-ig"') : page.index('aria-labelledby="ch-fb"')
    ]
    assert '<div class="panel-actions">' in ig and "badge" in ig

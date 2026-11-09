"""Home and Insights polish (2026-11-09 review): two independent columns on Home, a fixed slot
for each trend line, one quiet link style, the plan strip on the panel surface, distinct
activity icons, muted words instead of bare dashes, no boxed tables inside panels, and revenue
that never shows a bare "0"."""

from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.conversations.templatetags.insights_tags import figure

NOW = timezone.now()


@pytest.fixture
def shop(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _enquiry(account):
    contact = Contact.objects.create(account=account, phone="+260900000001")
    conv = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    Message.objects.create(
        account=account,
        conversation=conv,
        direction=Message.Direction.INBOUND,
        body="Hi",
        timestamp=NOW - timedelta(days=1),
    )


def _panels(client):
    return client.get("/dashboard/panels/", HTTP_HX_REQUEST="true").content.decode()


def test_home_is_two_columns_with_a_fixed_trend_slot(shop):
    client, _ = shop
    html = _panels(client)
    main = html[
        html.index('data-dash-column="main"') : html.index('data-dash-column="side"')
    ]
    side = html[html.index('data-dash-column="side"') :]
    assert 'aria-labelledby="attention-h"' in main and 'data-dash="activity"' in main
    assert 'data-dash="this-week"' in side and 'data-dash="coming-up"' in side
    week = side[
        side.index('data-dash="this-week"') : side.index('data-dash="coming-up"')
    ]
    # Every row keeps the slot, with or without a line, so the numbers line up.
    assert week.count('class="spark-slot"') == week.count("<li>")


def test_one_quiet_link_style_on_home_and_insights(shop):
    client, _ = shop
    html = _panels(client)
    assert 'class="link-quiet text-sm min-h-6">Insights' in html
    assert 'class="underline"' not in html and "font-medium underline" not in html


def test_activity_icons_are_distinct():
    from apps.core.icons import ICONS

    assert "megaphone" in ICONS and "tag" in ICONS
    source = Path("apps/accounts/home.py").read_text(encoding="utf-8")
    for icon in ('"icon": "image"', '"icon": "megaphone"', '"icon": "tag"'):
        assert icon in source


def test_empty_figures_read_in_words_and_revenue_never_shows_a_bare_zero(shop):
    client, account = shop
    _enquiry(account)
    assert "Not enough data" in figure("—") and figure("4 of 5") == "4 of 5"
    sales = client.get("/insights/?tab=sales").content.decode()
    tab = sales[sales.index('id="sales-h"') :]
    tab = tab[: tab.index("</section>")]
    assert "No paid orders yet" in tab and '<p class="metric-value">0</p>' not in tab
    assert 'class="metric-empty">Not enough data' in tab  # lead to sale, no leads


def test_a_tab_without_a_chart_is_one_full_width_row(shop):
    client, account = shop
    _enquiry(account)
    page = client.get("/insights/?tab=response").content.decode()
    assert "data-insights-chart" not in page
    figures = page[page.index('id="response-h"') :]
    assert figures.index('data-insights="chart-waiting"') < figures.index("metric-row")


def test_channel_table_has_no_box_inside_its_panel():
    html = Path("templates/insights/_channels.html").read_text(encoding="utf-8")
    assert "table-wrap" not in html and "panel-body--flush" in html

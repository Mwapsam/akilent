"""Insights on panels (2026-11-09 review, step 3): one header row, one "Early days" line,
panels, no repeated numbers, a segmented control whose switch swaps only the tab, and the
chart rules (only with enough data, weekly bars, the right unit, one label, dotted gaps)."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.template import Context, Template
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import insights_views
from apps.conversations.models import Conversation, Message
from apps.core import charts as geometry

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()


@pytest.fixture
def shop(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _enquiry(account, at, reply_after):
    contact = Contact.objects.create(
        account=account, phone=f"+2609{Contact.objects.count():08d}"
    )
    conv = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    Message.objects.create(
        account=account, conversation=conv, direction=IN, body="Hi", timestamp=at
    )
    Message.objects.create(
        account=account,
        conversation=conv,
        direction=OUT,
        body="Hello",
        status="sent",
        timestamp=at + reply_after,
    )


def test_one_header_row_and_one_early_days_line(shop):
    client, account = shop
    _enquiry(account, NOW - timedelta(days=2), timedelta(seconds=8))
    page = client.get("/insights/").content.decode()
    start = page.index('<header class="mb-5')
    header = page[start : page.index("</header>", start)]
    assert 'id="insights-controls"' in header and 'id="period"' in header
    assert "Updated" in header
    assert page.count('data-insights="early-days"') == 1
    assert "not enough data" not in page.lower()


def test_figures_are_panels_and_the_response_tab_repeats_nothing(shop):
    client, account = shop
    _enquiry(account, NOW - timedelta(days=2), timedelta(seconds=8))
    page = client.get("/insights/?tab=response").content.decode()
    assert '<section class="panel" aria-labelledby="headline-h"' in page
    assert '<section class="panel" aria-labelledby="attention-h"' in page
    tab = page[page.index('id="response-h"') :]
    tab = tab[: tab.index("</section>")]
    assert "First reply" not in tab and "Answered in 24h" not in tab
    assert "Missed" in tab and "Follow-ups done" in tab and "Time to resolve" in tab


def test_inner_tabs_are_a_segmented_control_that_swaps_only_the_tab(shop):
    client, account = shop
    page = client.get("/insights/").content.decode()
    nav = page[page.index('class="insights-tabs"') :]
    nav = nav[: nav.index("</nav>")]
    assert 'class="seg"' in nav and 'hx-target="#insights-tab"' in nav
    resp = client.get(
        "/insights/?tab=sales", HTTP_HX_REQUEST="true", HTTP_HX_TARGET="insights-tab"
    ).content.decode()
    assert 'data-insights-panel="sales"' in resp
    assert 'id="headline-h"' not in resp and 'id="attention-h"' not in resp
    # The header's period form follows the tab.
    assert (
        'id="insights-period-tab" name="tab" value="sales" hx-swap-oob="true"' in resp
    )


def test_a_quiet_refresh_brings_the_header_controls_with_it(shop):
    client, _ = shop
    partial = client.get("/insights/", HTTP_HX_REQUEST="true").content.decode()
    assert partial.lstrip().startswith('<div id="insights-body"')
    assert (
        'id="insights-controls" class="flex items-center gap-3" hx-swap-oob="true"'
        in partial
    )


def test_charts_wait_for_enough_data_and_counts_are_weekly_bars(shop):
    client, account = shop
    _enquiry(account, NOW - timedelta(days=2), timedelta(seconds=8))
    page = client.get("/insights/?tab=sales").content.decode()
    assert "Chart appears after a few more weeks of data." in page
    trend = {
        "unit": "Week",
        "labels": ["a", "b", "c", "d", "e"],
        "leads": [1, 2, 0, 3, 1],
        "paid": [0, 0, 1, 0, 0],
        "revenue": None,
    }
    charts = {c["key"]: c for c in insights_views._charts("sales", trend)}
    assert charts["leads"]["kind"] == "bars" and charts["leads"]["enough"]
    assert not charts["paid"]["enough"]  # one week with a sale isn't a trend


def test_reply_time_is_in_the_unit_that_fits():
    assert insights_views._reply_scale([8, None, 12]) == ("seconds", 1.0)
    assert insights_views._reply_scale([90, 600]) == ("minutes", 60.0)
    assert insights_views._reply_scale([3 * 3600]) == ("hours", 3600.0)
    trend = {
        "unit": "Week",
        "labels": list("abcd"),
        "reply_seconds": [8, 9, 7, 10],
        "reply_minutes": [0.1, 0.2, 0.1, 0.2],
        "answered": [1, 1, 1, 1],
        "missed": [0, 0, 0, 0],
    }
    reply = insights_views._charts("response", trend)[0]
    assert reply["title"] == "Median first reply, in seconds"
    assert reply["series"][0][1] == [8, 9, 7, 10]


def test_one_series_has_no_legend_and_gaps_are_dotted():
    html = Template(
        "{% load charts %}{% line_chart series labels unit='Week' %}"
    ).render(Context({"series": [("Reply", [1, None, 3, 4])], "labels": list("abcd")}))
    assert "chart-legend" not in html and "data-chart-gap" in html
    assert 'class="btn btn-ghost btn-sm">View as table' in html
    points = geometry.scale_series(
        [1, None, 3], width=100, height=50, low=0, high=4, pad=0
    )
    assert geometry._bridges(points).startswith("M")

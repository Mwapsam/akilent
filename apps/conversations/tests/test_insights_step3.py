"""Insights step 3 (2026-11-09): charts.

- one chart per tab over the chosen period (days, or weeks for 90 days), with a switcher when
  the tab has more than one metric; one unit per chart, never two axes;
- a day with no replies is a gap in the reply-time line, never a zero;
- funnel bars for sales and for posts, each step against the one before ("42%", or "3 of 4"
  from a handful), and "Not tracked yet" for a step nobody measures, never a zero.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.template import Context, Template
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import reporting
from apps.conversations.insights_views import _charts
from apps.conversations.models import Conversation, Message

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()


@pytest.fixture
def shop(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _enquiry(account, at, reply_after=None, sent_by=""):
    contact = Contact.objects.create(
        account=account, phone=f"+26097{Contact.objects.count():07d}"
    )
    conv = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    Message.objects.create(
        account=account, conversation=conv, direction=IN, body="Hi", timestamp=at
    )
    if reply_after is not None:
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=OUT,
            body="Hello",
            status="sent",
            timestamp=at + reply_after,
            metadata={"sent_by": sent_by} if sent_by else {},
        )


# --- The series -----------------------------------------------------------------------


def test_days_for_a_month_with_gaps_not_zeros(shop):
    _, account = shop
    _enquiry(account, NOW - timedelta(days=3), timedelta(minutes=6))
    _enquiry(account, NOW - timedelta(days=3), timedelta(minutes=2), sent_by="ai")
    _enquiry(account, NOW - timedelta(days=5))  # missed
    t = reporting.trend(account, reporting.last_days(30, NOW), now=NOW)
    assert not t["weekly"] and len(t["labels"]) in (30, 31)
    assert all(len(t[k]) == len(t["labels"]) for k in ("answered", "missed", "leads"))
    assert sum(t["answered"]) == 2 and sum(t["missed"]) == 1
    assert sum(t["automatic"]) == 1 and sum(t["people"]) == 1
    assert [m for m in t["reply_minutes"] if m is not None] == [4.0]
    assert t["reply_minutes"].count(None) == len(t["labels"]) - 1
    assert t["revenue"] is None


def test_weeks_for_ninety_days(shop):
    _, account = shop
    t = reporting.trend(account, reporting.last_days(90, NOW), now=NOW)
    assert t["weekly"] and t["unit"] == "Week" and 13 <= len(t["labels"]) <= 15
    assert t["labels"][0].startswith("w/c ")


def test_each_chart_has_one_unit_and_matching_columns(shop):
    _, account = shop
    t = reporting.trend(account, reporting.last_days(7, NOW), now=NOW)
    for tab, keys in (
        ("response", ["reply", "answered"]),
        ("sales", ["leads", "paid"]),
        ("automation", ["first"]),
    ):
        charts = _charts(tab, t)
        assert [c["key"] for c in charts] == keys
        for c in charts:
            assert len(c["series"]) <= 2
            assert all(len(values) == len(c["labels"]) for _, values in c["series"])
    assert _charts("content", t) == []


# --- The page -------------------------------------------------------------------------


def test_a_tab_with_two_metrics_has_a_switcher_and_one_without_has_none(shop):
    client, account = shop
    _enquiry(account, NOW - timedelta(days=2), timedelta(minutes=3))
    page = client.get("/insights/?tab=response").content.decode()
    chart = page[page.index("data-insights-chart") :]
    assert "chartSwitch('response', ['reply', 'answered'])" in chart
    assert 'class="seg" role="group" aria-label="Chart" x-cloak' in chart
    assert "Median first reply, in minutes" in chart and "View as table" in chart

    page = client.get("/insights/?tab=automation").content.decode()
    chart = page[page.index("data-insights-chart") :]
    assert 'class="seg"' not in chart[: chart.index("</section>")]


def test_the_sales_tab_draws_its_funnel(shop):
    client, account = shop
    _enquiry(account, NOW - timedelta(days=2), timedelta(minutes=3))
    page = client.get("/insights/?tab=sales").content.decode()
    funnel = page[page.index('aria-label="From conversation to sale"') :]
    assert "Wrote in" in funnel and "Paid orders" in funnel
    assert 'class="funnel-bar"' in funnel


# --- Funnel bars ----------------------------------------------------------------------


def _funnel(steps):
    return Template("{% load charts %}{% funnel steps label='F' %}").render(
        Context({"steps": steps})
    )


def test_funnel_rates_are_honest_about_small_numbers():
    html = _funnel(
        [
            {"label": "Wrote in", "value": 4},
            {"label": "Interested", "value": 3},
            {"label": "Paid", "value": None},
        ]
    )
    assert '<span class="funnel-rate">· 3 of 4</span>' in html  # not "75%"
    assert "Not tracked yet" in html

    html = _funnel([{"label": "A", "value": 50}, {"label": "B", "value": 21}])
    assert "· 42%" in html and "width: max(42.0%" in html

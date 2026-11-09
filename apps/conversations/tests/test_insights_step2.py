"""Insights step 2 (2026-11-09): the layout.

- four headline numbers, each against the period before it and this month's goal;
- "What needs you": at most three lines from live counts and observations, nothing said twice,
  each with when it started; the same list on Home, without the set-up gaps;
- Response / Sales / Content / Automation tabs, only the open one's figures worked out.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations import attention, reporting
from apps.conversations.models import Conversation, InsightGoal, Message
from apps.insights.models import Insight

IN, OUT = Message.Direction.INBOUND, Message.Direction.OUTBOUND
NOW = timezone.now()


@pytest.fixture
def shop(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _enquiry(account, at, reply_after=None):
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
        )
    return conv


def _insight(account, type_, count=1, severity=Insight.Severity.OPPORTUNITY):
    return Insight.objects.create(
        account=account,
        type=type_,
        severity=severity,
        title=f"{count} things ({type_})",
        evidence_count=count,
        suggested_action={"action": "inbox", "label": "Open inbox"},
    )


# --- What needs you -------------------------------------------------------------------


def test_observations_the_live_counts_already_say_are_left_out(shop):
    _, account = shop
    _enquiry(account, NOW - timedelta(hours=3))  # waiting
    _insight(account, "unanswered_conversations", severity=Insight.Severity.WARNING)
    _insight(account, "lead_followup_gap", severity=Insight.Severity.WARNING)
    keys = [i.key for i in attention.attention(account, now=NOW)["items"]]
    assert "waiting" in keys
    assert not [k for k in keys if k.startswith("insight:")]


def test_instagram_and_facebook_pairs_are_one_line(shop):
    _, account = shop
    _insight(account, "instagram_high_engagement_contact", count=3)
    _insight(account, "facebook_high_engagement_contact", count=1)
    items = attention.attention(account, now=NOW, include_setup=False)["items"]
    engaged = [i for i in items if i.key == "family:engaged"]
    assert len(engaged) == 1
    assert (
        engaged[0].text
        == "4 engaged contacts worth a message (Facebook 1, Instagram 3)"
    )
    assert len(engaged[0].insight_pks) == 2


def test_at_most_three_lines_with_the_rest_counted_and_the_start_known(shop):
    _, account = shop
    _enquiry(account, NOW - timedelta(days=2))  # missed
    _enquiry(account, NOW - timedelta(hours=2))  # waiting
    result = attention.attention(account, now=NOW)  # + hours, automations set-up gaps
    assert len(result["items"]) == attention.LIMIT
    assert result["more"] == result["total"] - attention.LIMIT > 0
    first = result["items"][0]
    assert first.key == "missed" and first.tone == "coral"
    assert abs((first.since - (NOW - timedelta(days=2))).total_seconds()) < 5


# --- Headline -------------------------------------------------------------------------


def test_the_headline_compares_counts_and_carries_this_months_goal(shop):
    _, account = shop
    for _ in range(reporting.MIN_SAMPLE):
        _enquiry(account, NOW - timedelta(days=2), reply_after=timedelta(minutes=2))
        _enquiry(account, NOW - timedelta(days=40), reply_after=timedelta(minutes=10))
    InsightGoal.objects.create(
        account=account, metric=InsightGoal.Metric.MEDIAN_FIRST_REPLY, target=5
    )
    goals = reporting.goal_progress(account, now=NOW)
    h = reporting.headline(account, reporting.last_days(30, NOW), now=NOW, goals=goals)
    assert h["reply"]["value"] == 120 and h["reply"]["change"] == -480
    assert h["answered"]["change"] == 0
    assert h["reply"]["goal"]["metric"] == InsightGoal.Metric.MEDIAN_FIRST_REPLY
    assert h["leads"]["change"] == 0 and h["paid"]["change"] == 0


# --- The page -------------------------------------------------------------------------


@pytest.mark.parametrize("tab", ["response", "sales", "content", "automation"])
def test_each_tab_renders_with_the_headline_and_what_needs_you(shop, tab):
    client, _ = shop
    page = client.get(f"/insights/?tab={tab}").content.decode()
    assert 'data-insights="headline"' in page and "What needs you" in page
    assert f'data-insights-panel="{tab}"' in page
    assert f'aria-current="page" data-insights-tab="{tab}"' in page
    # Nothing repeats below the tabs: observations are in the drawer, goals in another.
    assert 'id="observations"' not in page and 'id="momentum-h"' not in page
    assert 'href="/insights/goals/" data-overlay-open="drawer"' in page


def test_only_the_open_tab_is_worked_out(shop):
    client, _ = shop
    page = client.get("/insights/?tab=sales").content.decode()
    assert 'id="sales-h"' in page and 'id="response-h"' not in page
    assert 'id="automation-h"' not in page
    resp = client.get("/insights/?tab=nonsense")
    assert 'data-insights-panel="response"' in resp.content.decode()


def test_home_shows_what_needs_you_without_the_setup_gaps(shop):
    client, account = shop
    _enquiry(account, NOW - timedelta(hours=2))  # waiting
    panels = client.get("/dashboard/panels/", HTTP_HX_REQUEST="true").content.decode()
    assert "What needs you" in panels and 'data-attention="waiting"' in panels
    assert 'data-attention="hours"' not in panels
    assert 'data-attention="automations"' not in panels

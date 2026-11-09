"""Insights loose ends from the 2026-11-09 production walkthrough.

- an insight whose rule no longer finds anything closes itself;
- the headline sentence uses the figures' unit (conversations, with customers when different);
- a goal on a rate below the minimum sample is "too early to tell", never "on track";
- the labels stay on one line; nothing repeats below the tabs; the full list is a drawer.
"""

from datetime import timedelta

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, InsightGoal, Message
from apps.insights.engine import run_all_rules
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


def _conversation(account, contact, at, reply_after):
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


def test_an_insight_that_stopped_being_true_closes_itself(shop):
    _, account = shop
    stale = Insight.objects.create(
        account=account,
        type="unanswered_conversations",
        severity=Insight.Severity.WARNING,
        title="1 unanswered conversation needs attention",
    )
    summary = run_all_rules(account)  # nobody is waiting now
    stale.refresh_from_db()
    assert stale.status == Insight.Status.RESOLVED and stale.resolved_at is not None
    assert summary["resolved"] >= 1


def test_the_sentence_counts_conversations_and_names_customers_when_different(shop):
    client, account = shop
    ada = Contact.objects.create(account=account, phone="+260970000001")
    ben = Contact.objects.create(account=account, phone="+260970000002")
    _conversation(account, ada, NOW - timedelta(days=3), timedelta(minutes=2))
    _conversation(account, ada, NOW - timedelta(days=2), timedelta(minutes=2))
    _conversation(account, ben, NOW - timedelta(days=2), timedelta(minutes=2))
    page = client.get("/insights/").content.decode()
    assert "3 conversations got a reply (2 customers)" in page
    assert "From 3 conversations, not enough for a trend" in page


def test_a_goal_on_a_rate_from_a_handful_is_too_early_to_tell(shop):
    client, account = shop
    ada = Contact.objects.create(account=account, phone="+260970000001")
    _conversation(account, ada, NOW - timedelta(days=3), timedelta(minutes=2))
    InsightGoal.objects.create(
        account=account, metric=InsightGoal.Metric.ANSWERED_PCT, target=100
    )
    page = client.get("/insights/").content.decode()
    assert "Goal 100% · too early to tell" in page
    assert "Goal 100% · on track" not in page


def test_the_figures_have_short_one_line_labels():
    css = (settings.BASE_DIR / "assets" / "app.css").read_text(encoding="utf-8")
    rule = css[css.index("  .metric-label {") :]
    assert "whitespace-nowrap truncate" in rule[: rule.index("}")]
    src = (settings.BASE_DIR / "templates" / "insights" / "_headline.html").read_text(
        encoding="utf-8"
    )
    for label in ("First reply", "Answered in 24h", "Leads", "Paid orders"):
        assert f'<p class="metric-label">{label}</p>' in src


def test_the_rest_of_what_needs_you_is_a_drawer_on_insights_and_home(shop):
    client, account = shop
    for i in range(5):
        Insight.objects.create(
            account=account,
            type=f"t{i}",
            severity=Insight.Severity.OPPORTUNITY,
            title=f"Thing {i}",
        )
    page = client.get("/insights/").content.decode()
    assert 'href="/insights/needs-you/" data-overlay-open="drawer"' in page
    drawer = client.get("/insights/needs-you/").content.decode()
    assert all(f"Thing {i}" in drawer for i in range(5))
    assert "Dismiss" in drawer

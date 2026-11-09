"""Home on panels (2026-11-09 Dashboard review, step 2): "All caught up" instead of two 0s,
one "+ New" menu, Needs you naming the person and keeping old items behind "See all", This
week with trend lines, a real activity feed, Coming up with sends and posts, and the
starting point as one muted line."""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts import home
from apps.accounts.models import Account, Membership
from apps.contacts.models import Contact
from apps.conversations.attention import attention
from apps.conversations.models import Conversation, Message
from apps.insights.models import Insight

NOW = timezone.now()


@pytest.fixture
def owner(db, client):
    user = User.objects.create_user("o", "o@example.com", "pw")
    account = Account.objects.create(company_name="Mwamba Kitchen")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def _chat(account, first="Samuel", last="Chanda", minutes_ago=5, replied=False):
    contact = Contact.objects.create(
        account=account,
        first_name=first,
        last_name=last,
        phone=f"+2609{Contact.objects.count():08d}",
    )
    conv = Conversation.objects.create(
        account=account, contact=contact, channel="whatsapp"
    )
    Message.objects.create(
        account=account,
        conversation=conv,
        direction=Message.Direction.INBOUND,
        body="Hi",
        timestamp=NOW - timedelta(minutes=minutes_ago),
    )
    if replied:
        Message.objects.create(
            account=account,
            conversation=conv,
            direction=Message.Direction.OUTBOUND,
            body="Hello",
            status="sent",
            timestamp=NOW - timedelta(minutes=minutes_ago - 1),
        )
    return conv


def test_nothing_waiting_is_all_caught_up_not_two_zeros(owner):
    client, _ = owner
    page = client.get("/dashboard/").content.decode()
    assert 'data-tile="caught-up"' in page and "All caught up" in page
    assert 'href="/inbox/?view=missed"' not in page


def test_one_new_menu_replaces_the_quick_actions(owner):
    client, _ = owner
    page = client.get("/dashboard/").content.decode()
    assert "Go to Inbox" not in page and "Follow-up automation" not in page
    assert ">New</span>" in page and 'href="/contacts/create/"' in page
    assert "👋" not in page


def test_needs_you_names_the_one_customer_waiting(owner):
    _, account = owner
    conv = _chat(account)
    items = attention(account, include_setup=False, home=True)["items"]
    waiting = next(i for i in items if i.key == "waiting")
    assert waiting.text == "Waiting for a reply · Samuel C."
    assert waiting.url.endswith(f"/{conv.public_id}/") and waiting.cta == "Open"


def test_old_observations_wait_behind_see_all_on_home(owner):
    _, account = owner
    old = Insight.objects.create(
        account=account,
        type="t_old",
        severity=Insight.Severity.OPPORTUNITY,
        title="Something from three weeks ago",
    )
    Insight.objects.filter(pk=old.pk).update(created_at=NOW - timedelta(days=21))
    Insight.objects.create(
        account=account,
        type="t_new",
        severity=Insight.Severity.OPPORTUNITY,
        title="Fresh",
    )
    result = attention(account, include_setup=False, home=True)
    texts = [i.text for i in result["items"]]
    assert "Fresh" in texts and "Something from three weeks ago" not in texts
    assert result["more"] == 1
    # Insights keeps the full list.
    assert len(attention(account, include_setup=False)["items"]) == 2


def test_this_week_has_four_numbers_and_no_line_of_zeros(owner):
    _, account = owner
    _chat(account, minutes_ago=60 * 24 * 2, replied=True)
    _chat(account, first="Ada", minutes_ago=60 * 24 * 4, replied=True)
    items = home.this_week(account, NOW)["items"]
    assert [i["label"] for i in items] == [
        "Conversations",
        "First reply",
        "Leads",
        "Paid orders",
    ]
    assert items[0]["value"] == 2 and items[0]["spark"]
    assert items[2]["spark"] == []  # no leads: no flat line


def test_recent_activity_is_what_happened(owner):
    client, account = owner
    _chat(account)
    feed = home.recent_activity(account, NOW)
    assert feed and feed[0]["text"] == "Samuel C. wrote on WhatsApp"
    panels = client.get("/dashboard/panels/", HTTP_HX_REQUEST="true").content.decode()
    assert 'data-dash="activity"' in panels and "Samuel C. wrote on WhatsApp" in panels
    assert "Recent email sends" not in panels


def test_coming_up_merges_sends_and_posts_soonest_first(owner):
    _, account = owner
    sends = [
        {"when": NOW + timedelta(days=2), "label": "Promo", "channel": "whatsapp"},
        {"when": NOW + timedelta(hours=3), "label": "Reminder", "channel": "email"},
    ]
    rows = home.coming_up(account, sends, NOW)
    assert [r["label"] for r in rows] == ["Reminder", "Promo"]


def test_setup_and_plan_stay_off_home_when_nothing_needs_them(owner):
    client, _ = owner
    panels = client.get("/dashboard/panels/", HTTP_HX_REQUEST="true").content.decode()
    assert "All clear. Nothing to configure." not in panels
    assert "Business plan" not in panels and 'id="dash-plan"' not in panels

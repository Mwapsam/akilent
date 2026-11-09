"""Insights trust fixes (2026-11-09 review, step 1): funnels only ever narrow, one source for
chats from posts, and "missed" is a count (0, never a dash)."""

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.accounts.management.commands.seed_demo_workspace import DEMO_SLUG, OWNER_EMAIL
from apps.accounts.models import Account
from apps.conversations import reporting
from apps.conversations.models import ConversationEntryPoint
from apps.core import channels as ch
from apps.publishing.models import ContentPost
from apps.publishing.results import journey

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _channels(settings):
    settings.WHATSAPP_ENABLED = True
    settings.INSTAGRAM_ENABLED = True
    settings.FACEBOOK_ENABLED = True
    ch.invalidate_channel_cache()
    yield
    ch.invalidate_channel_cache()


@pytest.fixture
def demo():
    with override_settings(DEBUG=True):
        call_command("seed_demo_workspace", password="pw-demo-123")
    return Account.objects.get(slug=DEMO_SLUG)


def test_narrowing_counts_later_stages_as_earlier_ones():
    # 1 wrote in, nobody tracked as interested, but they're in the pipeline and paid.
    assert reporting.narrowing([{1, 2, 3}, set(), {1}, set(), {1, 9}]) == [
        3,
        1,
        1,
        1,
        1,
    ]
    # Someone outside the first stage (9) is never counted further down.
    assert reporting.narrowing([{1}, {9}]) == [1, 0]
    assert reporting.narrowing([]) == []


def test_sales_funnel_never_widens(demo):
    f = reporting.sales(demo, reporting.last_days(30))["funnel"]
    steps = [f["wrote_in"], f["leads"], f["deals"], f["won"], f["paid"]]
    assert steps == sorted(steps, reverse=True), steps


def test_a_chat_through_two_posts_is_one_chat_credited_to_the_first(demo):
    period = reporting.last_days(30)
    before = journey(demo, period)
    assert sum(p.chats for p in before["top"]) == before["chats"]
    # The same customer then taps a second post's link: still one chat.
    ep = ConversationEntryPoint.objects.filter(account=demo, post__isnull=False).first()
    other = ContentPost.objects.filter(account=demo).exclude(pk=ep.post_id).first()
    ConversationEntryPoint.objects.create(
        account=demo,
        conversation=ep.conversation,
        kind=ConversationEntryPoint.Kind.TRACKED_LINK,
        post=other,
    )
    after = journey(demo, period)
    assert after["chats"] == before["chats"]
    assert sum(p.chats for p in after["top"]) == after["chats"]
    assert after["chats"] >= after["leads"] >= after["orders"]


def test_clicks_are_in_the_sentence_not_a_funnel_step(demo, client):
    from django.contrib.auth.models import User

    client.force_login(User.objects.get(email=OWNER_EMAIL))
    html = client.get("/insights/?tab=content").content.decode()
    assert "chat-link click" in html
    assert "Chat link clicks" not in html


def test_none_missed_reads_zero(client):
    from django.contrib.auth.models import User

    from apps.accounts.models import Membership

    user = User.objects.create_user("m@example.com", "m@example.com", "pw")
    account = Account.objects.create(company_name="Missed Co")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    html = client.get("/insights/?tab=response").content.decode()
    figure = html[html.index('data-figure="missed"') :]
    assert figure[figure.index(">") + 1 :].startswith("0<")
    assert "none missed" not in html

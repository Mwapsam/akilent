"""Insights shows the whole customer journey from posts to paid orders, from the same
explicit evidence as each post's results card, and says "Not tracked yet" rather than 0
when a step can't be measured."""

from decimal import Decimal

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.accounts.management.commands.seed_demo_workspace import DEMO_SLUG, OWNER_EMAIL
from apps.accounts.models import Account, Membership
from apps.conversations import reporting
from apps.core import channels as ch
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


@pytest.fixture
def user_account():
    from django.contrib.auth.models import User

    user = User.objects.create_user("j@example.com", "j@example.com", "pw")
    account = Account.objects.create(
        company_name="Journey Co", selected_services="whatsapp"
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return user, account


def test_the_journey_adds_up_across_posts(demo):
    j = journey(demo, reporting.last_days(30))
    assert j["posts"] == 2
    assert j["chats"] == 3 and j["leads"] == 3 and j["orders"] == 3
    assert j["clicks"] > 0
    assert [r["total"] for r in j["revenue"]] == [Decimal("105")]
    assert [p.chats for p in j["top"]] == [2, 1]


def test_insights_shows_the_journey_with_links_to_each_post(demo, client):
    from django.contrib.auth.models import User

    client.force_login(User.objects.get(email=OWNER_EMAIL))
    html = client.get("/insights/").content.decode()
    assert "Your customer journey" in html
    assert "Posts that started chats" in html
    for post in journey(demo, reporting.last_days(30))["top"]:
        assert f'href="/publishing/posts/{post.pk}/"' in html


def test_untracked_steps_say_so_instead_of_zero(user_account):
    _, account = user_account
    j = journey(account, reporting.last_days(30))
    assert j["posts"] == 0
    assert j["clicks"] is None and j["chats"] is None and j["revenue"] is None


def test_untracked_steps_render_with_a_way_to_start(user_account, client):
    user, _ = user_account
    client.force_login(user)
    html = client.get("/insights/").content.decode()
    assert "Not tracked yet" in html
    assert "Add a WhatsApp chat link to a post" in html


def test_no_journey_without_somewhere_to_publish(user_account, settings):
    _, account = user_account
    settings.INSTAGRAM_ENABLED = False
    settings.FACEBOOK_ENABLED = False
    ch.invalidate_channel_cache()
    assert journey(account, reporting.last_days(30)) is None

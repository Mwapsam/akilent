"""Phase 2b (``cs_analytics``): each channel's account numbers a day at a time, with
states instead of zeros; the page, CSV and printable report; and the weekly email."""

import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, BusinessHours, Membership
from apps.core import channels as ch
from apps.publishing import account_metrics
from apps.publishing.metrics import State
from apps.publishing.metrics_collector import MetricsRateLimited
from apps.publishing.models import AccountMetricSnapshot, PublishingSettings
from apps.publishing.providers.errors import MetaPublishError

pytestmark = pytest.mark.django_db

LONDON = ZoneInfo("Europe/London")
NOW = datetime.datetime(2099, 3, 9, 9, 0, tzinfo=LONDON)  # a Monday
DAY = datetime.date(2099, 3, 7)  # NOW's settled day (two days back)


@pytest.fixture(autouse=True)
def _channels(settings):
    settings.INSTAGRAM_ENABLED = True
    settings.FACEBOOK_ENABLED = True
    ch.invalidate_channel_cache()
    cache.clear()
    yield
    ch.invalidate_channel_cache()


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({account_metrics.FLAG})


class Shop:
    def __init__(self, name="shop", role=Membership.Role.OWNER):
        self.account = Account.objects.create(company_name=name)
        BusinessHours.objects.create(account=self.account, timezone="Europe/London")
        self.user = User.objects.create_user(name, f"{name}@x.example", "pass")
        Membership.objects.create(user=self.user, account=self.account, role=role)
        self.client = Client()
        self.client.force_login(self.user)


@pytest.fixture
def shop():
    return Shop()


def _ig(**insights):
    provider = MagicMock()
    provider.fetch_media_fields.return_value = {"followers_count": 1200}
    provider.fetch_account_insights.return_value = insights or {
        "reach": 500,
        "views": 900,
        "accounts_engaged": 40,
        "total_interactions": 60,
    }
    return provider


def _collect(shop, provider, platform="instagram", day=DAY, now=NOW):
    with (
        patch.object(account_metrics, "connection", return_value=(provider, "ig_1")),
        patch("django.utils.timezone.now", return_value=now),
    ):
        return account_metrics.collect_day(shop.account, platform, day)


# --- Collecting ----------------------------------------------------------------------------


def test_a_day_is_read_with_its_own_bounds_on_the_business_clock(shop):
    provider = _ig()

    snap = _collect(shop, provider)

    assert snap.values == {
        "followers": 1200,
        "reach": 500,
        "views": 900,
        "engaged": 40,
        "interactions": 60,
    }
    assert set(snap.states.values()) == {State.AVAILABLE}
    args = provider.fetch_account_insights.call_args.args
    assert args[0] == "ig_1"
    assert args[1] == ["reach", "views", "accounts_engaged", "total_interactions"]
    start = datetime.datetime(2099, 3, 7, tzinfo=LONDON)
    assert (args[2], args[3]) == (
        int(start.timestamp()),
        int((start + datetime.timedelta(days=1)).timestamp()),
    )


def test_a_missing_number_stays_missing_not_zero(shop):
    snap = _collect(shop, _ig(reach=500))

    assert snap.values["views"] is None
    assert snap.states["views"] == State.PENDING
    assert snap.states["reach"] == State.AVAILABLE


def test_an_invalid_metric_is_found_by_asking_one_at_a_time(shop):
    provider = _ig()

    def insights(target, metrics, since, until):
        if len(metrics) > 1 or metrics == ["accounts_engaged"]:
            raise MetaPublishError("invalid metric", code=100, http_status=400)
        return {metrics[0]: 7}

    provider.fetch_account_insights.side_effect = insights

    snap = _collect(shop, provider)

    assert snap.states["engaged"] == State.UNSUPPORTED
    assert snap.values["reach"] == 7 and snap.states["reach"] == State.AVAILABLE


def test_a_revoked_token_asks_for_a_reconnect(shop):
    provider = _ig()
    provider.fetch_account_insights.side_effect = MetaPublishError(
        "expired", code=190, http_status=401
    )

    snap = _collect(shop, provider)

    assert snap.states["reach"] == State.NEEDS_PERMISSION
    assert snap.states["followers"] == State.AVAILABLE


def test_a_rate_limit_stops_the_run(shop):
    provider = _ig()
    provider.fetch_account_insights.side_effect = MetaPublishError(
        "slow down", code=4, http_status=400
    )

    with pytest.raises(MetricsRateLimited):
        _collect(shop, provider)


def test_facebook_reads_the_page(shop):
    provider = MagicMock()
    provider.fetch_fields.return_value = {"followers_count": 300}
    provider.fetch_page_insights.return_value = {
        "page_total_media_view_unique": 80,
        "page_media_view": 150,
    }

    snap = _collect(shop, provider, platform="facebook")

    assert snap.values == {"followers": 300, "reach": 80, "views": 150}
    assert provider.fetch_page_insights.call_args.args[0] == [
        "page_total_media_view_unique",
        "page_media_view",
    ]


def test_not_connected_asks_for_a_reconnect(shop):
    with patch("django.utils.timezone.now", return_value=NOW):
        snap = account_metrics.collect_day(shop.account, "instagram", DAY)

    assert set(snap.states.values()) == {State.NEEDS_PERMISSION}


def test_an_older_day_read_again_keeps_its_followers(shop):
    older = DAY - datetime.timedelta(days=3)
    provider = _ig()

    snap = _collect(shop, provider, day=older)

    provider.fetch_media_fields.assert_not_called()
    assert snap.values["followers"] is None
    assert snap.values["reach"] == 500


def test_days_to_collect_retries_only_incomplete_days(shop):
    complete = dict.fromkeys(
        ("reach", "views", "engaged", "interactions"), State.AVAILABLE
    )
    AccountMetricSnapshot.objects.create(
        account=shop.account,
        platform="instagram",
        day=DAY,
        states=complete,
        collected_at=NOW,
    )
    AccountMetricSnapshot.objects.create(
        account=shop.account,
        platform="instagram",
        day=DAY - datetime.timedelta(days=1),
        states={**complete, "views": State.PENDING},
        collected_at=NOW,
    )

    days = account_metrics.days_to_collect(shop.account, "instagram", now=NOW)

    assert DAY not in days
    assert DAY - datetime.timedelta(days=1) in days
    assert len(days) == account_metrics.RETRY_DAYS - 1


def test_the_task_does_nothing_until_released(shop):
    from apps.publishing.tasks import collect_account_metrics

    with patch.object(account_metrics, "collect_day") as collect:
        assert collect_account_metrics.run() == 0
    collect.assert_not_called()


# --- Reading it back ---------------------------------------------------------------------


def _day(shop, day, platform="instagram", **values):
    defs = account_metrics.account_metrics_for(platform)
    AccountMetricSnapshot.objects.create(
        account=shop.account,
        platform=platform,
        day=day,
        values={m.key: values.get(m.key) for m in defs},
        states={
            m.key: State.AVAILABLE if values.get(m.key) is not None else State.PENDING
            for m in defs
        },
        collected_at=NOW,
    )


def test_reach_is_averaged_views_are_added_followers_show_the_change(shop):
    _day(shop, DAY - datetime.timedelta(days=2), followers=1000, reach=100, views=300)
    _day(shop, DAY - datetime.timedelta(days=1), followers=None, reach=300, views=500)
    _day(shop, DAY, followers=1030, reach=None, views=None)

    s = account_metrics.summary(shop.account, "instagram", 7, now=NOW)

    rows = {r["key"]: r for r in s["rows"]}
    assert rows["reach"]["average"] == 200 and rows["reach"]["total"] is None
    assert rows["reach"]["days_with_data"] == 2
    assert rows["views"]["total"] == 800 and rows["views"]["average"] == 400
    assert (rows["followers"]["last"], rows["followers"]["change"]) == (1030, 30)
    assert rows["engaged"]["days_with_data"] == 0
    assert rows["engaged"]["reason"].startswith("No number from Instagram yet")


def test_the_page_and_exports(shop, released):
    today = timezone.localdate()
    settled = account_metrics.settled_day(shop.account)
    _day(shop, settled, followers=1200, reach=500, views=None)

    page = shop.client.get(reverse("publishing:analytics"))
    csv_resp = shop.client.get(reverse("publishing:analytics-csv") + "?period=7")
    printed = shop.client.get(reverse("publishing:analytics-print"))

    html = page.content.decode()
    assert page.status_code == 200
    assert 'data-analytics="instagram"' in html
    assert 'href="/publishing/analytics/"' in html  # the Analytics tab
    body = csv_resp.content.decode()
    assert csv_resp["Content-Type"].startswith("text/csv")
    assert f"{settled},instagram,Reach,500,available" in body
    assert f"{settled},instagram,Views,,pending" in body  # blank, never 0
    assert "window.print()" in printed.content.decode()
    assert today  # the page renders on today's clock


def test_analytics_is_hidden_until_released(shop):
    assert shop.client.get(reverse("publishing:analytics")).status_code == 404
    assert shop.client.get(reverse("publishing:analytics-csv")).status_code == 404


# --- Weekly email ------------------------------------------------------------------------


def test_only_owners_turn_the_weekly_email_on(released):
    owner, member = Shop("owner"), Shop("member", role=Membership.Role.MEMBER)
    url = reverse("publishing:analytics-weekly")

    owner.client.post(url, {"weekly_report": "on"})
    member.client.post(url, {"weekly_report": "on"})

    assert PublishingSettings.objects.get(account=owner.account).weekly_report
    assert not PublishingSettings.objects.filter(
        account=member.account, weekly_report=True
    ).exists()


def test_the_weekly_email_goes_once_on_monday_morning(shop):
    PublishingSettings.objects.create(account=shop.account, weekly_report=True)
    _day(shop, DAY, followers=1200, reach=500, views=900)

    with patch("apps.accounts.notifications.notify_team") as notify:
        sunday = account_metrics.send_weekly_reports(NOW - datetime.timedelta(days=1))
        early = account_metrics.send_weekly_reports(NOW.replace(hour=7))
        first = account_metrics.send_weekly_reports(NOW)
        again = account_metrics.send_weekly_reports(NOW + datetime.timedelta(hours=1))

    assert (sunday, early, first, again) == (0, 0, 1, 0)
    kwargs = notify.call_args.kwargs
    assert kwargs["to"] == "owners"
    assert "Followers: 1,200" in kwargs["text"]
    assert "Views: 900 in total" in kwargs["text"]
    assert "the least your posts brought in" in kwargs["text"]


def test_a_failed_weekly_email_is_tried_again_and_a_cache_flush_doesnt_resend(shop):
    PublishingSettings.objects.create(account=shop.account, weekly_report=True)
    _day(shop, DAY, followers=1200, reach=500, views=900)

    with patch(
        "apps.accounts.notifications.notify_team", side_effect=[OSError("smtp"), None]
    ):
        failed = account_metrics.send_weekly_reports(NOW)
        retried = account_metrics.send_weekly_reports(NOW + datetime.timedelta(hours=1))
    cache.clear()
    with patch("apps.accounts.notifications.notify_team") as notify:
        after_flush = account_metrics.send_weekly_reports(
            NOW + datetime.timedelta(hours=2)
        )

    assert (failed, retried, after_flush) == (0, 1, 0)
    notify.assert_not_called()

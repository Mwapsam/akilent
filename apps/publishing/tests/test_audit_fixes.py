"""Fixes from the 2026-11-11 browser audit of Content Studio (bugs 1-4, 7)."""

import datetime
import importlib

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, BusinessHours, Membership
from apps.publishing import readiness
from apps.publishing.models import ContentPlanItem, ContentPost, ContentPostVariant

pytestmark = pytest.mark.django_db


@pytest.fixture
def shop():
    account = Account.objects.create(company_name="Audit Shop")
    user = User.objects.create_user("audit", "audit@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client


# --- 1. "Write the post" -------------------------------------------------------------


def test_write_the_post_starts_from_the_ideas_words(shop):
    account, client = shop
    item = ContentPlanItem.objects.create(
        account=account,
        topic="Week 2: Mid-month offer reminder",
        objective="Remind people the offer ends Friday",
        platforms=["instagram"],
    )

    response = client.post(reverse("publishing:plan-create-post", args=[item.pk]))

    post = ContentPost.objects.get(plan_item=item)
    assert response["Location"] == f"{reverse('publishing:post-new')}?post_id={post.pk}"
    assert post.caption == (
        "Week 2: Mid-month offer reminder\n\nRemind people the offer ends Friday"
    )
    # The idea's words don't hide "Start with AI".
    html = client.get(response["Location"]).content.decode()
    assert "Start with AI" in html


def test_clicking_write_the_post_twice_opens_the_same_draft(shop):
    account, client = shop
    item = ContentPlanItem.objects.create(
        account=account, topic="Once only", platforms=["instagram"]
    )
    url = reverse("publishing:plan-create-post", args=[item.pk])

    first = client.post(url)
    second = client.post(url)

    assert ContentPost.objects.filter(plan_item=item).count() == 1
    assert first["Location"] == second["Location"]


def test_the_write_buttons_show_they_are_working(shop):
    account, client = shop
    ContentPlanItem.objects.create(
        account=account,
        topic="Soon",
        planned_date=timezone.localdate() + datetime.timedelta(days=1),
    )

    for name in ("publishing:index", "publishing:planning"):
        html = client.get(reverse(name)).content.decode()
        forms = [ln for ln in html.splitlines() if "/create-post/" in ln]
        assert forms and all(
            'hx-boost="false"' in f and "data-busy-submit" in f for f in forms
        ), name


# --- 2. Old failed posts: each channel gets its own outcome ---------------------------


def _repair():
    module = importlib.import_module(
        "apps.publishing.migrations.0028_repair_backfilled_channel_failures"
    )
    module.repair(django_apps, None)


def test_old_failed_post_gives_each_channel_its_own_reason(shop):
    account, _ = shop
    post = ContentPost.objects.create(
        account=account,
        caption="x",
        platforms=["instagram", "facebook"],
        status=ContentPost.Status.FAILED,
        error="Instagram: Unsupported aspect ratio (500x1080, ratio 0.46).; "
        "Facebook: Page token expired",
    )
    ig = ContentPostVariant.objects.create(
        post=post, platform="instagram", status="failed"
    )
    fb = ContentPostVariant.objects.create(
        post=post, platform="facebook", status="failed"
    )

    _repair()

    ig.refresh_from_db()
    fb.refresh_from_db()
    assert ig.last_error == "Unsupported aspect ratio (500x1080, ratio 0.46)."
    assert ig.last_error_type == "validation"
    assert fb.last_error == "Page token expired"


def test_old_failed_post_channel_that_went_live_is_published(shop):
    account, _ = shop
    post = ContentPost.objects.create(
        account=account,
        caption="x",
        platforms=["instagram", "facebook"],
        status=ContentPost.Status.FAILED,
        error="Instagram: Unsupported aspect ratio",
        fb_post_id="123_456",
    )
    fb = ContentPostVariant.objects.create(
        post=post, platform="facebook", status="failed", publication_id="123_456"
    )

    _repair()

    fb.refresh_from_db()
    assert fb.status == "published"
    assert fb.published_at is not None


def test_a_channel_with_no_part_of_the_error_says_so_honestly(shop):
    account, _ = shop
    post = ContentPost.objects.create(
        account=account,
        caption="x",
        platforms=["instagram", "facebook"],
        status=ContentPost.Status.FAILED,
        error="Instagram: Unsupported aspect ratio",
    )
    fb = ContentPostVariant.objects.create(
        post=post, platform="facebook", status="failed"
    )

    _repair()

    fb.refresh_from_db()
    assert "no reason for this channel" in fb.last_error


# --- 3. One clock across Content Studio ------------------------------------------------


def test_every_content_studio_page_uses_the_business_clock(shop):
    account, client = shop
    BusinessHours.objects.create(account=account, timezone="Africa/Lusaka")  # UTC+2
    seen = {}

    from apps.publishing import views

    original = views.render

    def spy(request, template, context=None, *a, **kw):
        seen["tz"] = timezone.get_current_timezone_name()
        return original(request, template, context, *a, **kw)

    views.render = spy
    try:
        client.get(reverse("publishing:planning"))  # never had the decorator
    finally:
        views.render = original
    assert seen["tz"] == "Africa/Lusaka"


# --- 4. A picture Instagram refuses is stopped before it's sent -----------------------


def _failed_ig_post(account, url="https://media.example.com/tall.jpg"):
    post = ContentPost.objects.create(
        account=account,
        caption="x",
        platforms=["instagram"],
        media_type="image",
        media_urls=[url],
        status=ContentPost.Status.FAILED,
    )
    ContentPostVariant.objects.create(
        post=post, platform="instagram", status="failed", last_error="ratio"
    )
    return post


def test_retry_refuses_a_picture_instagram_will_refuse(shop, monkeypatch):
    account, client = shop
    post = _failed_ig_post(account)
    monkeypatch.setattr(
        "apps.publishing.media_library.image_size", lambda account, url: (500, 1080)
    )
    queued = []
    monkeypatch.setattr(
        "apps.publishing.tasks.publish_variant_async.delay",
        lambda pk: queued.append(pk),
    )

    client.post(reverse("publishing:post-retry-variant", args=[post.pk, "instagram"]))
    client.post(reverse("publishing:post-retry", args=[post.pk]))

    assert queued == []
    post.refresh_from_db()
    assert post.status == ContentPost.Status.FAILED
    assert post.variants.get().status == "failed"


def test_retry_goes_ahead_when_the_picture_fits(shop, monkeypatch):
    account, client = shop
    post = _failed_ig_post(account)
    monkeypatch.setattr(
        "apps.publishing.media_library.image_size", lambda account, url: (1080, 1350)
    )
    queued = []
    monkeypatch.setattr(
        "apps.publishing.tasks.publish_variant_async.delay",
        lambda pk: queued.append(pk),
    )

    client.post(reverse("publishing:post-retry-variant", args=[post.pk, "instagram"]))

    assert queued == [post.variants.get().pk]


def test_composer_check_reads_the_size_of_a_picture_not_in_the_library(
    shop, monkeypatch
):
    account, _ = shop
    post = _failed_ig_post(account)
    monkeypatch.setattr(
        "apps.publishing.media_library._read_image_size", lambda url: (500, 1080)
    )
    monkeypatch.setattr("apps.publishing.services._is_own_media_url", lambda url: True)

    assert "500 by 1080" in readiness.ig_ratio_problem(post)


# --- 7. A planned day that passed ------------------------------------------------------


def test_a_planned_day_that_passed_is_missed(shop):
    account, client = shop
    item = ContentPlanItem.objects.create(
        account=account,
        topic="Last Tuesday's post",
        planned_date=timezone.localdate() - datetime.timedelta(days=3),
        plan_status=ContentPlanItem.PlanStatus.PLANNED,
    )
    assert item.is_missed

    planning = client.get(reverse("publishing:planning")).content.decode()
    overview = client.get(reverse("publishing:index")).content.decode()

    assert "Last Tuesday&#x27;s post" in planning and "1 missed" in planning
    assert "Last Tuesday&#x27;s post" in overview and "Missed" in overview


def test_a_started_post_is_not_missed():
    item = ContentPlanItem(
        topic="t",
        planned_date=timezone.localdate() - datetime.timedelta(days=3),
        plan_status=ContentPlanItem.PlanStatus.IN_PRODUCTION,
    )
    assert not item.is_missed


# --- Numbers that disagreed ------------------------------------------------------------


def test_a_dated_idea_is_planned_and_an_undated_one_is_an_idea(shop):
    account, _ = shop
    item = ContentPlanItem.objects.create(
        account=account, topic="t", planned_date=timezone.localdate()
    )
    assert item.plan_status == ContentPlanItem.PlanStatus.PLANNED

    item.planned_date = None
    item.save(update_fields=["planned_date"])
    item.refresh_from_db()
    assert item.plan_status == ContentPlanItem.PlanStatus.IDEA


def test_an_idea_is_published_when_its_post_goes_out(shop):
    from apps.publishing.services import refresh_parent_post

    account, _ = shop
    item = ContentPlanItem.objects.create(account=account, topic="t")
    post = ContentPost.objects.create(
        account=account,
        caption="x",
        platforms=["facebook"],
        plan_item=item,
        status=ContentPost.Status.PUBLISHING,
    )
    ContentPostVariant.objects.create(
        post=post, platform="facebook", status="published"
    )

    refresh_parent_post(post)

    item.refresh_from_db()
    assert item.plan_status == ContentPlanItem.PlanStatus.PUBLISHED


def test_the_calendar_counts_planned_ideas_as_planning_does(shop):
    account, client = shop
    today = timezone.localdate()
    ContentPlanItem.objects.create(account=account, topic="a", planned_date=today)
    ContentPlanItem.objects.create(
        account=account,
        topic="b",
        planned_date=today,
        plan_status=ContentPlanItem.PlanStatus.PUBLISHED,
    )

    html = client.get(
        reverse("publishing:calendar") + f"?month={today:%Y-%m}"
    ).content.decode()

    assert "1 planned" in html


def test_reach_says_why_a_channel_has_no_number():
    from apps.publishing.templatetags.publishing_filters import reach_summary

    ig = ContentPostVariant(platform="instagram", status="published")
    fb = ContentPostVariant(
        platform="facebook",
        status="published",
        metrics_latest={"values": {"reach": 340}},
        metrics_collected_at=timezone.now(),
    )

    class _Variants:
        def all(self):
            return [ig, fb]

    holder = type("P", (), {"variants": _Variants()})()
    assert reach_summary(holder) == "Reach: Instagram not read yet · Facebook 340"


def test_a_live_channel_never_read_is_read(shop, monkeypatch):
    from apps.publishing import tasks

    account, _ = shop
    post = ContentPost.objects.create(
        account=account, caption="x", platforms=["facebook"], status="published"
    )
    v = ContentPostVariant.objects.create(
        post=post, platform="facebook", status="published"
    )
    queued = []
    monkeypatch.setattr(
        tasks.collect_variant_metrics_task, "delay", lambda pk: queued.append(pk)
    )

    with TestCase.captureOnCommitCallbacks(execute=True):
        tasks.collect_due_metrics()

    assert queued == [v.pk]


# --- UX polish -------------------------------------------------------------------------


def test_published_this_month_links_to_those_posts(shop):
    _, client = shop
    html = client.get(reverse("publishing:index")).content.decode()
    assert 'data-bucket="published-month"' in html
    assert "show=published&amp;date_from=" in html


def test_planning_stat_cards_filter_the_list(shop):
    account, client = shop
    ContentPlanItem.objects.create(account=account, topic="Undated idea")
    ContentPlanItem.objects.create(
        account=account, topic="Dated one", planned_date=timezone.localdate()
    )

    html = client.get(reverse("publishing:planning") + "?status=idea").content.decode()

    assert 'data-plan-filter="idea"' in html
    assert "Undated idea" in html and "Dated one" not in html
    assert 'aria-current="true"' in html


def test_library_shows_when_a_post_goes_out_and_filters_by_it(shop):
    account, client = shop
    old = timezone.now() - datetime.timedelta(days=40)
    post = ContentPost.objects.create(
        account=account,
        caption="",
        platforms=["facebook"],
        status=ContentPost.Status.PUBLISHED,
        published_at=timezone.now(),
    )
    ContentPost.objects.filter(pk=post.pk).update(created_at=old)
    today = timezone.localdate().isoformat()

    html = client.get(
        reverse("publishing:library") + f"?date_from={today}&date_to={today}"
    ).content.decode()

    assert f"/posts/{post.pk}/" in html  # published today, though written 40 days ago
    assert ">When<" in html and "Published" in html
    assert "No caption · " in html and "(no caption)" not in html


def test_instagram_can_be_disconnected_from_channels(shop, monkeypatch):
    from apps.instagram.models import InstagramBusinessAccount

    account, client = shop
    iba = InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id="178",
        username="shop",
        access_token="tok",
        is_active=True,
    )
    monkeypatch.setattr(
        "apps.instagram.oauth.unsubscribe_ig_account_from_webhooks", lambda *a: None
    )

    html = client.get(reverse("publishing:channels")).content.decode()
    assert "Account: @shop" in html and "Disconnect Instagram" in html
    assert "Publishing on" not in html

    client.post(reverse("publishing:ig-disconnect"))
    iba.refresh_from_db()
    assert not iba.is_active


def test_post_images_open_in_the_viewer_not_the_media_host(shop):
    account, client = shop
    post = ContentPost.objects.create(
        account=account,
        caption="Two pictures",
        platforms=["instagram"],
        media_type="carousel",
        media_urls=["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
    )
    ContentPostVariant.objects.create(post=post, platform="instagram")

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()

    links = [ln for ln in html.splitlines() if 'href="https://cdn.example.com/' in ln]
    assert len(links) == 2
    assert all(
        f'data-viewer-group="post-{post.pk}"' in ln and "_blank" not in ln
        for ln in links
    )

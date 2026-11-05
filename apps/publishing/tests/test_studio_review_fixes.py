"""Fixes from the Content Studio walkthrough: failed posts can be fixed, not only retried;
planned ideas due soon show under "Needs you"; counts and labels agree; Instagram's picture
shape is checked before publishing; every Content Studio page shows the workspace."""

import datetime
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing import media_library, readiness
from apps.publishing.composer import ComposerInput
from apps.publishing.models import (
    ContentPlanItem,
    ContentPost,
    ContentPostVariant,
    MediaAsset,
)
from apps.publishing.templatetags.publishing_filters import platform_names

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status
E = ContentPostVariant.ErrorType


@pytest.fixture
def shop():
    account = Account.objects.create(company_name="Review Shop")
    user = User.objects.create_user("rev", "rev@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client


def _failed(account, statuses=(S.FAILED, S.FAILED), error_type=E.VALIDATION):
    post = ContentPost.objects.create(
        account=account,
        caption="Launch",
        platforms=["instagram", "facebook"],
        media_type="image",
        media_urls=["https://example.com/a.jpg"],
        status=ContentPost.Status.FAILED,
        error="Instagram: Unsupported aspect ratio",
    )
    for platform, status in zip(("instagram", "facebook"), statuses, strict=True):
        ContentPostVariant.objects.create(
            post=post,
            platform=platform,
            status=status,
            last_error="Unsupported aspect ratio" if platform == "instagram" else "",
            last_error_type=error_type if platform == "instagram" else "",
        )
    return post


# --- A failed post can be fixed ----------------------------------------------------------


def test_a_refused_post_offers_edit_and_fix_first(shop):
    account, client = shop
    post = _failed(account)

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()

    assert reverse("publishing:post-fix", args=[post.pk]) in html
    assert "Edit and fix" in html
    assert "data-no-reason" in html  # Facebook failed with no recorded reason


def test_edit_and_fix_reopens_it_as_a_draft_in_the_composer(shop):
    account, client = shop
    post = _failed(account)

    resp = client.post(reverse("publishing:post-fix", args=[post.pk]))

    assert resp.url == f"{reverse('publishing:post-new')}?post_id={post.pk}"
    post.refresh_from_db()
    assert post.status == ContentPost.Status.DRAFT and post.error == ""
    ig = post.variants.get(platform="instagram")
    assert ig.status == S.DRAFT and ig.last_error  # kept as a hint
    assert client.get(resp.url).status_code == 200


def test_a_post_partly_out_cant_be_reopened(shop):
    account, client = shop
    post = _failed(account, statuses=(S.FAILED, S.PUBLISHED))

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()
    client.post(reverse("publishing:post-fix", args=[post.pk]))

    assert reverse("publishing:post-fix", args=[post.pk]) not in html
    post.refresh_from_db()
    assert post.status == ContentPost.Status.FAILED


# --- Instagram's picture shape before publishing -----------------------------------------


def test_a_too_tall_picture_is_caught_before_instagram_refuses_it(shop):
    account, _ = shop
    url = "https://media.example/tall.png"
    MediaAsset.objects.create(
        account=account,
        url=url,
        url_digest=media_library.url_digest(url),
        kind="image",
        width=498,
        height=1080,
    )
    inp = ComposerInput(
        platforms=["instagram"], media_type="image", media_urls=[url], caption="Hi"
    )

    issue = next(
        i
        for i in readiness.check(inp, account, wa_ready=True)
        if i["code"] == "ig_ratio"
    )

    assert "498 by 1080" in issue["message"]
    assert readiness.SCHEDULE in issue["blocks"]
    inp.platforms = ["facebook"]
    assert "ig_ratio" not in {
        i["code"] for i in readiness.check(inp, account, wa_ready=True)
    }


# --- Overview and Planning ---------------------------------------------------------------


def test_planned_ideas_due_soon_need_you(shop):
    account, client = shop
    today = timezone.localdate()
    soon = ContentPlanItem.objects.create(
        account=account,
        topic="Team intro",
        planned_date=today + datetime.timedelta(days=2),
    )
    ContentPlanItem.objects.create(
        account=account,
        topic="Far off",
        planned_date=today + datetime.timedelta(days=20),
    )
    written = ContentPlanItem.objects.create(
        account=account, topic="Done", planned_date=today + datetime.timedelta(days=1)
    )
    ContentPost.objects.create(
        account=account, plan_item=written, platforms=["instagram"]
    )

    html = client.get(reverse("publishing:index")).content.decode()

    assert f'data-plan-due="{soon.pk}"' in html
    assert "Team intro" in html and "Far off" not in html
    assert f'data-plan-due="{written.pk}"' not in html
    assert "Nothing is waiting on you" not in html


def test_overview_tiles_are_named_and_empty_ones_collapse(shop):
    account, client = shop
    _failed(account)

    html = client.get(reverse("publishing:index")).content.decode()

    assert 'aria-label="Failed: 1 post"' in html
    assert 'data-bucket="draft"' not in html
    assert "Nothing in:" in html and "Drafts" in html


def test_planning_counts_agree_with_the_lists(shop):
    account, client = shop
    ContentPlanItem.objects.create(
        account=account, topic="Dated idea", planned_date=timezone.localdate()
    )

    html = client.get(reverse("publishing:planning")).content.decode()

    assert ">Idea<" in html  # one idea, singular
    assert "Not on the calendar yet" in html
    assert "Everything you've planned has a date" in html
    assert "No ideas yet" not in html


def test_saving_a_dated_idea_says_when(shop):
    _, client = shop
    resp = client.post(
        reverse("publishing:plan-new"),
        {"topic": "Promo", "planned_date": "2026-12-03", "plan_status": "idea"},
        follow=True,
    )
    assert "Planned for Thu, Dec. 3." in resp.content.decode()


# --- Labels and the workspace name -------------------------------------------------------


def test_channel_names_are_capitalised():
    assert platform_names(["instagram", "facebook"]) == "Instagram, Facebook"


@pytest.mark.parametrize(
    "flag,url",
    [
        ("cs_media_library", "/publishing/media/"),
        ("cs_sales_report", "/publishing/sales/"),
        ("cs_ai_plus", "/publishing/ai/"),
        ("cs_bio_page", "/publishing/bio/"),
    ],
)
def test_every_studio_page_shows_the_workspace(shop, settings, flag, url):
    _, client = shop
    settings.RELEASED_FEATURES = frozenset({flag})

    html = client.get(url).content.decode()

    assert "Review Shop" in html, json.dumps(url)


# --- Phase 0 of the app-feel plan --------------------------------------------------------


def test_a_post_published_straight_away_shows_on_the_calendar(shop):
    account, client = shop
    sent = timezone.now() - datetime.timedelta(days=1)
    post = ContentPost.objects.create(
        account=account,
        caption="Went out now",
        platforms=["instagram"],
        status=ContentPost.Status.PUBLISHED,
        published_at=sent,
    )
    ContentPostVariant.objects.create(
        post=post, platform="instagram", status=S.PUBLISHED, published_at=sent
    )
    day = timezone.localtime(sent).date()

    week = client.get(
        reverse("publishing:calendar")
        + f"?week={day.isocalendar()[0]}-W{day.isocalendar()[1]:02d}"
    )
    days = {d: posts for d, _, posts in week.context["week_days"]}

    assert [e["post"].pk for e in days[day]] == [post.pk]


def test_the_plan_form_returns_to_where_it_was_opened(shop):
    account, client = shop
    item = ContentPlanItem.objects.create(account=account, topic="Teaser")
    back = reverse("publishing:calendar") + "?view=month&month=2026-11"
    url = reverse("publishing:plan-edit", args=[item.pk])

    page = client.get(url, {"next": back}).content.decode()
    saved = client.post(url, {"topic": "Teaser 2", "plan_status": "idea", "next": back})
    evil = client.post(
        url, {"topic": "x", "plan_status": "idea", "next": "//evil.example/"}
    )

    assert "← Calendar" in page and 'name="next"' in page
    assert saved.url == back
    assert evil.url == reverse("publishing:planning")


def test_plan_an_idea_from_a_day_fills_in_the_date(shop):
    _, client = shop
    html = client.get(
        reverse("publishing:plan-new"), {"planned_date": "2026-12-10"}
    ).content.decode()
    assert 'value="2026-12-10"' in html


def test_deleting_a_plan_item_uses_its_own_form(shop):
    account, client = shop
    item = ContentPlanItem.objects.create(account=account, topic="Gone")
    html = client.get(reverse("publishing:plan-edit", args=[item.pk])).content.decode()

    assert 'form="plan-delete-form"' in html
    assert html.count("<form") == html.count("</form>")
    form = html.split('id="plan-delete-form"')[0].rsplit("<form", 1)[0]
    assert form.count("<form") == form.count("</form>")  # not nested in the save form


# --- Phase 1: no forced full reloads -----------------------------------------------------

BOOSTED = {
    "HTTP_HX_REQUEST": "true",
    "HTTP_HX_BOOSTED": "true",
    "HTTP_X_AKILENT_SHELL": "app",
}


def test_the_composer_and_media_pages_swap_in_place(shop, settings):
    account, client = shop
    settings.RELEASED_FEATURES = frozenset({media_library.FLAG})
    url = "https://media.example/p.png"
    asset = MediaAsset.objects.create(
        account=account, url=url, url_digest=media_library.url_digest(url), kind="image"
    )

    for page in (
        reverse("publishing:post-new"),
        reverse("publishing:media-library"),
        reverse("publishing:media-edit", args=[asset.pk]),
        reverse("publishing:calendar"),
    ):
        resp = client.get(page, **BOOSTED)
        assert resp.status_code == 200, page
        assert not resp.has_header("HX-Refresh") and not resp.has_header(
            "HX-Redirect"
        ), page
        assert resp.get("HX-Push-Url") == page


def test_the_page_scripts_load_with_the_shell(shop):
    _, client = shop
    html = client.get(reverse("publishing:index")).content.decode()
    for script in ("js/cropper.min.js", "js/composer.js", "js/media_library.js"):
        assert script in html


def _seed(account, n, start=0):
    now = timezone.now()
    for i in range(start, start + n):
        status = ("draft", "scheduled", "published", "failed")[i % 4]
        when = None if status == "draft" else now + datetime.timedelta(days=i % 6 - 2)
        post = ContentPost.objects.create(
            account=account,
            caption=f"post {i}",
            platforms=["instagram", "facebook"],
            media_type="image",
            media_urls=[f"https://m.example/{i}.jpg"],
            status=status,
            scheduled_at=when,
        )
        for platform in ("instagram", "facebook"):
            ContentPostVariant.objects.create(
                post=post,
                platform=platform,
                status=status,
                scheduled_at=when,
                published_at=now if status == "published" else None,
            )
        ContentPlanItem.objects.create(
            account=account, topic=f"plan {i}", planned_date=now.date()
        )
        url = f"https://m.example/asset{i}.jpg"
        MediaAsset.objects.create(
            account=account,
            url=url,
            url_digest=media_library.url_digest(url),
            kind="image",
        )


@pytest.mark.parametrize(
    "url",
    [
        "/publishing/",
        "/publishing/planning/",
        "/publishing/calendar/",
        "/publishing/calendar/?view=month",
        "/publishing/library/",
        "/publishing/media/",
        "/publishing/media/?sort=used",
        "/publishing/media/?show=unused&view=list",
        "/publishing/sales/",
    ],
)
def test_tab_queries_dont_grow_with_the_number_of_posts(shop, settings, url):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    account, client = shop
    settings.RELEASED_FEATURES = frozenset(
        {"cs_media_library", "cs_sales_report", "cs_scheduling"}
    )
    _seed(account, 4)
    client.get(url)
    with CaptureQueriesContext(connection) as few:
        assert client.get(url).status_code == 200
    _seed(account, 16, start=4)
    with CaptureQueriesContext(connection) as many:
        client.get(url)

    assert len(many) - len(few) <= 2, [q["sql"][:90] for q in many.captured_queries]

"""Content Studio in place: a post opens as a drawer over the list it was clicked in, and
"Add idea" / "Edit plan item" open over the page, save without leaving it, and close.

The overlay host is static/js/app.js "Overlay"; the server side is overlay_or_page and
overlay_done in apps/core/htmx.py. Each URL stays a real page."""

import datetime
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPlanItem, ContentPost, ContentPostVariant

pytestmark = pytest.mark.django_db

OVERLAY = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": "overlay-body"}


@pytest.fixture
def shop():
    account = Account.objects.create(company_name="Overlay Shop")
    user = User.objects.create_user("ov", "ov@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client


def _post(account, status=ContentPost.Status.DRAFT, **kw):
    post = ContentPost.objects.create(
        account=account,
        caption=kw.pop("caption", "Autumn drop"),
        platforms=["instagram"],
        media_type="image",
        media_urls=["https://example.com/a.jpg"],
        status=status,
        **kw,
    )
    ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=ContentPostVariant.Status.FAILED
        if status == ContentPost.Status.FAILED
        else ContentPostVariant.Status.DRAFT,
    )
    return post


def _is_fragment(response):
    html = response.content.decode()
    assert "<html" not in html.lower()
    assert 'id="main"' not in html
    assert response["X-Akilent-Overlay"] == "1"
    # The same URL answers a full page: never cached, and caches keyed on these headers.
    assert response["Cache-Control"] == "no-store"
    vary = response["Vary"]
    assert "HX-Request" in vary and "HX-Target" in vary
    return html


# --- The post drawer ---------------------------------------------------------------------


def test_the_post_opens_as_a_drawer_from_the_overlay(shop):
    account, client = shop
    post = _post(account)

    response = client.get(reverse("publishing:post-detail", args=[post.pk]), **OVERLAY)

    html = _is_fragment(response)
    assert 'data-overlay="drawer"' in html
    assert 'data-overlay-size="lg"' in html
    assert f'data-post-drawer="{post.pk}"' in html
    assert "Open full page" in html


def test_the_post_url_is_still_the_full_page(shop):
    account, client = shop
    post = _post(account)

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()

    assert 'id="main"' in html
    assert "data-post-drawer" not in html
    assert 'id="overlay-host"' in html  # the host is in every signed-in page


def test_a_failed_post_can_be_fixed_or_retried_from_the_drawer(shop):
    account, client = shop
    post = _post(account, status=ContentPost.Status.FAILED, error="Bad ratio")

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk]), **OVERLAY
    ).content.decode()

    assert reverse("publishing:post-fix", args=[post.pk]) in html
    assert reverse("publishing:post-retry", args=[post.pk]) in html
    assert "Bad ratio" in html


def test_retrying_in_the_drawer_shows_the_drawer_again_with_a_toast(shop):
    """A drawer action whose view redirects back to the post: the browser follows the
    redirect with the same headers, so the drawer re-renders and the message rides along."""
    account, client = shop
    post = _post(account, status=ContentPost.Status.FAILED, error="Timeout")

    response = client.post(
        reverse("publishing:post-retry", args=[post.pk]), follow=True, **OVERLAY
    )

    html = _is_fragment(response)
    assert f'data-post-drawer="{post.pk}"' in html
    assert response["X-Toast"].startswith("success|")


def test_a_drawer_action_that_leaves_the_post_loads_that_page_for_real(shop):
    """Delete redirects to Overview, which can't show itself in the overlay: go there with a
    real page load, with the message kept for it."""
    account, client = shop
    post = _post(account)

    response = client.post(
        reverse("publishing:post-delete", args=[post.pk]), follow=True, **OVERLAY
    )

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("publishing:index")
    html = client.get(reverse("publishing:index")).content.decode()
    assert "Post deleted." in html


@pytest.mark.parametrize(
    "name",
    ["publishing:library", "publishing:index", "publishing:calendar"],
)
def test_lists_open_posts_in_the_drawer(shop, name):
    account, client = shop
    _post(account, status=ContentPost.Status.FAILED, error="x")
    soon = timezone.now() + datetime.timedelta(hours=2)
    sched = _post(account, status=ContentPost.Status.SCHEDULED, scheduled_at=soon)
    ContentPostVariant.objects.filter(post=sched).update(
        status=ContentPostVariant.Status.SCHEDULED, scheduled_at=soon
    )

    html = client.get(reverse(name)).content.decode()

    assert 'data-overlay-open="drawer"' in html
    assert "data-row" in html


# --- Add idea and Edit plan item ---------------------------------------------------------


def test_add_idea_opens_as_a_modal_with_the_day_filled_in(shop):
    _account, client = shop

    response = client.get(
        reverse("publishing:plan-new") + "?planned_date=2026-11-20", **OVERLAY
    )

    html = _is_fragment(response)
    assert 'data-overlay="modal"' in html
    assert 'data-overlay-title="Add idea"' in html
    assert 'value="2026-11-20"' in html
    assert "data-overlay-close" in html  # Cancel closes rather than navigating
    assert "autofocus" in html


def test_saving_an_idea_in_the_modal_closes_it(shop):
    account, client = shop

    response = client.post(
        reverse("publishing:plan-new"),
        {"topic": "Gift guide", "planned_date": "2026-11-20", "media_type": "image"},
        **OVERLAY,
    )

    assert response.status_code == 204
    assert json.loads(response["HX-Trigger"]) == {"overlay-close": True}
    assert response["X-Toast"].startswith("success|Planned%20for")
    item = ContentPlanItem.objects.get(account=account)
    assert item.topic == "Gift guide"
    assert str(item.planned_date) == "2026-11-20"


def test_a_refused_idea_stays_in_the_modal_with_its_reason(shop):
    _account, client = shop

    response = client.post(
        reverse("publishing:plan-new"),
        {"topic": "", "planned_date": "2026-11-20", "notes": "Keep me"},
        **OVERLAY,
    )

    assert response.status_code == 422
    html = _is_fragment(response)
    assert 'data-overlay="modal"' in html
    # The reason is under the field (a toast covered the modal's Close), and what was typed stays.
    assert 'aria-invalid="true" aria-describedby="plan-topic-error"' in html
    assert "Give the idea a topic." in html
    assert ">Keep me</textarea>" in html
    assert 'value="2026-11-20"' in html
    assert "X-Toast" not in response
    assert "ak-overlay-actions" in html  # buttons pinned to the modal's bottom
    assert not ContentPlanItem.objects.exists()


def test_the_plan_page_shows_the_same_field_errors(shop):
    _account, client = shop

    response = client.post(reverse("publishing:plan-new"), {"topic": " "})

    assert response.status_code == 422
    assert "Give the idea a topic." in response.content.decode()


def test_without_the_overlay_the_plan_form_still_redirects(shop):
    _account, client = shop
    nxt = reverse("publishing:calendar") + "?view=month"

    response = client.post(
        reverse("publishing:plan-new"),
        {"topic": "Gift guide", "next": nxt, "media_type": "image"},
    )

    assert response.status_code == 302
    assert response["Location"] == nxt


def test_editing_and_deleting_a_plan_item_in_the_drawer(shop):
    account, client = shop
    item = ContentPlanItem.objects.create(account=account, topic="Old")
    url = reverse("publishing:plan-edit", args=[item.pk])

    html = _is_fragment(client.get(url, **OVERLAY))
    assert 'data-overlay="drawer"' in html
    assert reverse("publishing:plan-delete", args=[item.pk]) in html

    saved = client.post(url, {"topic": "New", "media_type": "image"}, **OVERLAY)
    assert saved.status_code == 204
    item.refresh_from_db()
    assert item.topic == "New"

    deleted = client.post(reverse("publishing:plan-delete", args=[item.pk]), **OVERLAY)
    assert deleted.status_code == 204
    assert json.loads(deleted["HX-Trigger"]) == {"overlay-close": True}
    assert not ContentPlanItem.objects.exists()


@pytest.mark.parametrize(
    "name", ["publishing:planning", "publishing:calendar", "publishing:index"]
)
def test_plan_links_open_in_the_overlay(shop, name):
    account, client = shop
    ContentPlanItem.objects.create(
        account=account,
        topic="Due soon",
        planned_date=timezone.localdate() + datetime.timedelta(days=1),
        plan_status=ContentPlanItem.PlanStatus.PLANNED,
    )

    html = client.get(reverse(name)).content.decode()

    assert 'data-overlay-open="' in html
    for line in html.splitlines():
        if (
            "/publishing/plan/" in line
            and "href=" in line
            and "plan-create" not in line
        ):
            assert "data-overlay-open" in line, line


# --- The rest of Content Studio's side pages ---------------------------------------------

ALL_FLAGS = frozenset(
    {"cs_media_library", "cs_sales_report", "cs_scheduling", "cs_csv_import"}
)


def _asset(account):
    from apps.publishing import media_library
    from apps.publishing.models import MediaAsset

    url = "https://example.com/media/shop/pic.jpg"
    return MediaAsset.objects.create(
        account=account, url=url, url_digest=media_library.url_digest(url), kind="image"
    )


@pytest.mark.parametrize(
    "url,kind,title",
    [
        ("/publishing/pillars/", "modal", "Content pillars"),
        ("/publishing/campaigns/", "modal", "Content themes"),
        ("/publishing/slots/", "drawer", "Posting slots"),
        ("/publishing/import/", "modal", "Import posts from a CSV file"),
        ("/publishing/media/snippets/", "drawer", "Caption templates and hashtags"),
        ("/help/post-results/", "modal", None),
    ],
)
def test_side_pages_open_in_the_overlay_and_stay_pages(
    shop, settings, url, kind, title
):
    _account, client = shop
    settings.RELEASED_FEATURES = ALL_FLAGS

    html = _is_fragment(client.get(url, **OVERLAY))
    assert f'data-overlay="{kind}"' in html
    if title:
        assert f'data-overlay-title="{title}"' in html
    # Native-only forms on the page post back into the overlay instead.
    assert 'hx-boost="false"' not in html

    page = client.get(url).content.decode()
    assert "<html" in page.lower() and "data-overlay=" not in page


def test_the_media_editor_opens_as_a_wide_modal(shop, settings):
    account, client = shop
    settings.RELEASED_FEATURES = ALL_FLAGS
    asset = _asset(account)
    url = reverse("publishing:media-edit", args=[asset.pk])

    html = _is_fragment(client.get(url, **OVERLAY))

    assert 'data-overlay="modal"' in html and 'data-overlay-size="full"' in html
    assert "cropper.min" in html  # its stylesheet comes with it
    assert 'x-data="mediaEditor()"' in html
    grid = client.get(reverse("publishing:media-library")).content.decode()
    assert f'href="{url}" data-overlay-open="modal"' in grid


def test_a_pillar_saved_in_the_modal_comes_back_to_it(shop):
    account, client = shop

    response = client.post(
        "/publishing/pillars/", {"name": "Behind the scenes"}, follow=True, **OVERLAY
    )

    html = _is_fragment(response)
    assert "Behind the scenes" in html
    assert response["X-Toast"].startswith("success|")


def test_a_clean_csv_import_closes_the_modal_over_the_library(shop, settings):
    account, client = shop
    settings.RELEASED_FEATURES = ALL_FLAGS
    session = client.session
    session["publishing_csv_import"] = {"account": account.pk, "rows": []}
    session.save()

    response = client.post("/publishing/import/", {"action": "import"}, **OVERLAY)

    assert response.status_code == 204
    assert json.loads(response["HX-Trigger"]) == {"overlay-close": True}


@pytest.mark.parametrize(
    "url,needle",
    [
        (
            "/publishing/planning/",
            'href="/publishing/pillars/" data-overlay-open="modal"',
        ),
        (
            "/publishing/calendar/",
            'href="/publishing/slots/" data-overlay-open="drawer"',
        ),
        (
            "/publishing/library/",
            'href="/publishing/import/" data-overlay-open="modal"',
        ),
        (
            "/publishing/media/",
            'href="/publishing/media/snippets/" data-overlay-open="drawer"',
        ),
        (
            "/publishing/new/",
            'href="/publishing/media/snippets/" data-overlay-open="drawer"',
        ),
        ("/publishing/new/", 'href="/publishing/slots/" data-overlay-open="drawer"'),
    ],
)
def test_side_pages_are_opened_in_the_overlay(shop, settings, url, needle):
    _account, client = shop
    settings.RELEASED_FEATURES = ALL_FLAGS

    assert needle in client.get(url).content.decode()


def test_the_composer_is_never_refreshed_under_an_overlay(shop):
    _account, client = shop

    html = client.get("/publishing/new/").content.decode()

    assert 'data-overlay-refresh="off"' in html


# --- Review of Phases 0-2 ----------------------------------------------------------------

SHELL_SWAP = {
    "HTTP_HX_REQUEST": "true",
    "HTTP_HX_BOOSTED": "true",
    "HTTP_X_AKILENT_SHELL": "app",
}


def test_the_composer_brings_its_styles_and_layout_into_a_shell_swap(shop):
    """Reached from "New post" inside the app, the composer was unstyled: its <style> lived
    only in <head>, and <main> kept the previous page's classes."""
    _account, client = shop

    response = client.get(reverse("publishing:post-new"), **SHELL_SWAP)

    html = response.content.decode()
    assert not response.has_header("HX-Refresh")
    assert "<!DOCTYPE" not in html
    assert ".composer-main" in html  # composer/_styles.html came with the page
    assert "cropper.min.css" in html
    assert '<template data-main-class="composer-main' in html


def test_other_pages_send_the_default_main_classes(shop):
    _account, client = shop

    html = client.get(reverse("publishing:library"), **SHELL_SWAP).content.decode()

    assert '<template data-main-class="px-4 sm:px-6 lg:px-8 py-6' in html


def test_a_full_load_still_puts_page_styles_in_the_head(shop):
    _account, client = shop

    html = client.get(reverse("publishing:post-new")).content.decode()

    head, body = html.split("</head>", 1)
    assert ".composer-main" in head and ".composer-main" not in body
    assert '<main id="main" tabindex="-1" hx-history-elt class="composer-main' in body
    assert "data-main-class" not in body


@pytest.mark.parametrize(
    "url",
    [
        "/publishing/",
        "/publishing/library/",
        "/publishing/calendar/",
        "/publishing/planning/",
        "/publishing/channels/",
    ],
)
def test_every_tab_has_the_same_header_so_the_tabs_dont_move(shop, url):
    _account, client = shop

    html = client.get(url).content.decode()

    main = html.split('id="main"', 1)[1]
    before_tabs = main.split("data-instant-tabs", 1)[0]
    assert 'class="cs-head"' in before_tabs
    assert "<style" not in before_tabs
    assert (
        "text-ink-subtle mt-0.5" not in before_tabs
    )  # no subtitle changing its height


def test_today_is_marked_for_assistive_tech(shop):
    _account, client = shop

    week = client.get("/publishing/calendar/").content.decode()
    month = client.get("/publishing/calendar/?view=month").content.decode()

    assert 'aria-current="date"' in week
    assert 'aria-current="date"' in month


def test_full_pages_vary_on_the_overlay_target_too(shop):
    account, client = shop
    post = _post(account)

    response = client.get(reverse("publishing:post-detail", args=[post.pk]))

    assert "HX-Target" in response["Vary"]


def test_overviews_action_on_a_post_opens_the_drawer(shop):
    account, client = shop
    _post(account, status=ContentPost.Status.FAILED, error="x")

    html = client.get(reverse("publishing:index")).content.decode()

    for line in html.splitlines():
        if "/publishing/posts/" in line and "href=" in line:
            assert "data-overlay-open" in line, line


def test_an_idea_without_channels_still_becomes_a_post(shop):
    """Add idea doesn't ask for channels; "→ Post" and "Write the post" used to fail with
    "At least one platform must be selected"."""
    account, client = shop
    item = ContentPlanItem.objects.create(account=account, topic="No channels yet")

    response = client.post(reverse("publishing:plan-create-post", args=[item.pk]))

    post = ContentPost.objects.get(plan_item=item)
    assert response.status_code == 302
    assert f"post_id={post.pk}" in response["Location"]
    assert post.platforms  # the channels switched on, changeable in the composer
    assert post.variants.count() == len(post.platforms)


def test_the_editor_loads_its_picture_apart_from_the_grids_copy(shop, settings):
    """The browser kept the grid's copy of a picture (cached for a year, no CORS header) and
    handed it to the editor's CORS load, which failed: the editor asks for its own copy."""
    account, client = shop
    settings.RELEASED_FEATURES = ALL_FLAGS
    asset = _asset(account)

    html = client.get(
        reverse("publishing:media-edit", args=[asset.pk]), **OVERLAY
    ).content.decode()

    assert f'src="{asset.url}?cors=1" crossorigin="anonymous"' in html


def test_the_follow_ups_badge_survives_an_in_place_navigation(shop):
    """The nav is re-rendered out of band on every in-place navigation; a new badge fetched its
    count on load, twice per page (desktop and mobile navs)."""
    _account, client = shop

    html = client.get(reverse("publishing:library")).content.decode()

    assert 'id="nav-badge-follow-ups-desktop" hx-preserve' in html
    assert 'id="nav-badge-follow-ups-mobile" hx-preserve' in html

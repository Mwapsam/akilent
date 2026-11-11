"""Content Studio app feel, Phase 3: live filters with removable chips (Library, Media,
Calendar, Sales), media cards that remove in place, toasts whose Undo is the server's own,
and undoing a cancelled post (back to scheduled while its times are ahead, else a draft)."""

import datetime
from urllib.parse import unquote

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.publishing import media_library
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import undo_cancel
from apps.publishing.tests.test_media_library import MEDIA, Shop
from apps.publishing.tests.test_media_library import P as Post
from apps.publishing.tests.test_media_library import S as Variant
from apps.publishing.worklist import BUCKETS, bucket_url

pytestmark = pytest.mark.django_db

BACKGROUND = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_URL = MEDIA
    settings.MEDIA_ROOT = tmp_path
    settings.RELEASED_FEATURES = frozenset({media_library.FLAG})


@pytest.fixture
def shop():
    return Shop()


def _toast(resp):
    """(type, message, undo) from an X-Toast header."""
    parts = resp["X-Toast"].split("|")
    return parts[0], unquote(parts[1]), unquote(parts[2]) if len(parts) > 2 else None


def _scheduled(shop, minutes=60, platforms=("instagram",)):
    when = timezone.now() + datetime.timedelta(minutes=minutes)
    post = ContentPost.objects.create(
        account=shop.account,
        caption="Autumn drop",
        platforms=list(platforms),
        media_type="image",
        media_urls=["https://example.com/a.jpg"],
        status=ContentPost.Status.SCHEDULED,
        scheduled_at=when,
    )
    for p in platforms:
        ContentPostVariant.objects.create(
            post=post,
            platform=p,
            status=ContentPostVariant.Status.SCHEDULED,
            scheduled_at=when,
        )
    return post


# --- Toasts with the server's Undo --------------------------------------------------------


def test_cancelling_a_post_offers_undo(shop):
    post = _scheduled(shop)

    resp = shop.client.post(reverse("publishing:post-cancel", args=[post.pk]))

    message = next(iter(get_messages(resp.wsgi_request)))
    assert str(message) == "Post cancelled."
    assert f"undo:/publishing/posts/undo-cancel/?ids={post.pk}" in message.tags
    # The same from the background: the undo rides the toast header.
    other = _scheduled(shop)
    bg = shop.client.post(
        reverse("publishing:post-cancel", args=[other.pk]), **BACKGROUND
    )
    assert bg.status_code == 204
    assert _toast(bg)[2] == f"/publishing/posts/undo-cancel/?ids={other.pk}"


def test_undo_while_the_time_is_ahead_schedules_it_again(shop, monkeypatch):
    from apps.publishing import readiness

    monkeypatch.setattr(readiness, "check", lambda *a, **k: [])
    post = _scheduled(shop, minutes=90)
    when = post.variants.get().scheduled_at
    shop.client.post(reverse("publishing:post-cancel", args=[post.pk]))

    resp = shop.client.post(
        reverse("publishing:post-undo-cancel") + f"?ids={post.pk}", **BACKGROUND
    )

    post.refresh_from_db()
    variant = post.variants.get()
    assert post.status == ContentPost.Status.SCHEDULED
    assert variant.status == ContentPostVariant.Status.SCHEDULED
    assert variant.scheduled_at == when
    assert resp.status_code == 204 and "scheduled again" in _toast(resp)[1]


def test_undo_after_the_time_has_passed_brings_back_a_draft(shop):
    post = _scheduled(shop, minutes=1)
    shop.client.post(reverse("publishing:post-cancel", args=[post.pk]))

    resp = shop.client.post(
        reverse("publishing:post-undo-cancel") + f"?ids={post.pk}", **BACKGROUND
    )

    post.refresh_from_db()
    assert post.status == ContentPost.Status.DRAFT and post.scheduled_at is None
    level, message, _ = _toast(resp)
    assert level == "warning" and "back as a draft" in message
    assert "time has passed" in message


def test_undo_goes_through_the_readiness_check(shop, monkeypatch):
    from apps.publishing import readiness

    monkeypatch.setattr(
        readiness,
        "check",
        lambda *a, **k: [
            {"code": "x", "message": "Connect Instagram.", "blocks": ["schedule"]}
        ],
    )
    monkeypatch.setattr(readiness, "blocking", lambda issues, gate: list(issues))
    post = _scheduled(shop, minutes=90)
    shop.client.post(reverse("publishing:post-cancel", args=[post.pk]))

    outcome, reason = undo_cancel(ContentPost.objects.get(pk=post.pk))

    post.refresh_from_db()
    assert (outcome, reason) == ("draft", "Connect Instagram.")
    assert post.status == ContentPost.Status.DRAFT


def test_undo_does_nothing_to_a_post_that_moved_on(shop):
    post = _scheduled(shop)
    assert undo_cancel(post) == ("none", "")
    theirs = _scheduled(Shop("other"))
    theirs.status = ContentPost.Status.CANCELLED
    theirs.save()

    shop.client.post(reverse("publishing:post-undo-cancel") + f"?ids={theirs.pk}")

    theirs.refresh_from_db()
    assert theirs.status == ContentPost.Status.CANCELLED


def test_bulk_cancel_offers_one_undo_for_all(shop):
    a, b = _scheduled(shop), _scheduled(shop)

    resp = shop.client.post(
        reverse("publishing:library-bulk"),
        {"action": "cancel_scheduled", "post_ids": [a.pk, b.pk]},
    )

    tags = [m.tags for m in get_messages(resp.wsgi_request)]
    assert any(f"ids={a.pk},{b.pk}" in t or f"ids={b.pk},{a.pk}" in t for t in tags)


# --- Media cards in place ------------------------------------------------------------------


def test_removing_from_a_card_archives_in_place_with_undo(shop):
    asset = shop.asset()
    shop.post(asset.url, Post.PUBLISHED, Variant.PUBLISHED)
    url = reverse("publishing:media-update", args=[asset.pk])

    resp = shop.client.post(url, {"action": "remove", "card": "1"}, **BACKGROUND)

    asset.refresh_from_db()
    assert asset.archived_at is not None
    assert resp.status_code == 200 and resp.content == b""  # the card goes
    level, message, undo = _toast(resp)
    assert message.startswith("Archived") and undo == reverse(
        "publishing:media-restore", args=[asset.pk]
    )

    back = shop.client.post(undo, **BACKGROUND)
    asset.refresh_from_db()
    assert back.status_code == 204 and asset.archived_at is None


def test_a_card_a_draft_needs_stays_and_a_delete_has_no_undo(shop):
    needed = shop.asset("needed.png")
    shop.post(needed.url, Post.DRAFT)
    free = shop.asset("free.png", data=b"z" * 30)
    url = lambda a: reverse("publishing:media-update", args=[a.pk])  # noqa: E731

    kept = shop.client.post(
        url(needed), {"action": "remove", "card": "1"}, **BACKGROUND
    )
    gone = shop.client.post(url(free), {"action": "remove", "card": "1"}, **BACKGROUND)

    assert f'data-asset="{needed.pk}"' in kept.content.decode()
    assert _toast(kept)[0] == "danger"
    assert gone.content == b"" and _toast(gone) == ("success", "Deleted.", None)


# --- Live filters and chips ----------------------------------------------------------------


@pytest.mark.parametrize("key", [k for k, _ in BUCKETS])
def test_every_overview_bucket_link_still_filters_the_library(shop, key):
    resp = shop.client.get(bucket_url(key))

    assert resp.status_code == 200
    assert resp.context["show"] == key and resp.context["bucket_filter"] == key
    assert f'<option value="{key}" selected>' in resp.content.decode()


def test_the_library_has_one_show_control_and_chips(shop):
    published = _scheduled(shop)
    published.status = ContentPost.Status.PUBLISHED
    published.save()
    _scheduled(shop)

    resp = shop.client.get(
        reverse("publishing:library"), {"show": "published", "q": "Autumn"}
    )
    html = resp.content.decode()

    assert [p.pk for p in resp.context["page_obj"]] == [published.pk]
    assert (
        'data-live-filter="#library-results"' in html and 'id="library-results"' in html
    )
    assert 'name="status"' not in html and 'name="bucket"' not in html
    assert 'data-chip-clear="show bucket status"' in html
    assert (
        'data-chip-clear="q"' in html and "?show=published" in html
    )  # q's chip keeps show
    # ?status= links from before still work.
    old = shop.client.get(reverse("publishing:library"), {"status": "published"})
    assert [p.pk for p in old.context["page_obj"]] == [published.pk]


def test_media_calendar_and_sales_filter_in_place(shop, settings):
    settings.RELEASED_FEATURES = frozenset(
        {media_library.FLAG, "cs_sales_report", "cs_scheduling"}
    )
    shop.asset()

    media = shop.client.get(reverse("publishing:media-library"), {"show": "image"})
    calendar = shop.client.get(reverse("publishing:calendar"), {"channel": "facebook"})
    sales = shop.client.get(reverse("publishing:sales"))

    assert 'data-live-filter="#media-results"' in media.content.decode()
    # No chip for "show": the segmented control already says it (2026-11-11 audit).
    assert 'data-chip-clear="show kind archived"' not in media.content.decode()
    assert 'data-live-filter="#cal-results"' in calendar.content.decode()
    assert 'data-chip-clear="channel"' in calendar.content.decode()
    assert 'data-live-filter="#sales-results"' in sales.content.decode()

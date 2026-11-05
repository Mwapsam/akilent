"""The media library as a gallery (Content Studio app feel, Phases 4–5): clean names, the
filters (All/Images/Videos/Unused/Archived, search by name or tag, sort), duplicates, a
file's details as a drawer, the selection bar's bulk actions, and "Use in post"."""

import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.core.htmx import OVERLAY_HEADER, OVERLAY_TARGET
from apps.publishing import media_library
from apps.publishing.models import MediaAsset
from apps.publishing.tests.test_media_library import MEDIA, Shop
from apps.publishing.tests.test_media_library import P as Post
from apps.publishing.tests.test_media_library import S as Variant

pytestmark = pytest.mark.django_db

OVERLAY = {"HTTP_HX_REQUEST": "true", "HTTP_HX_TARGET": OVERLAY_TARGET}


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_URL = MEDIA
    settings.MEDIA_ROOT = tmp_path
    settings.RELEASED_FEATURES = frozenset({media_library.FLAG})


@pytest.fixture
def shop():
    return Shop()


def _ids(html):
    import re

    return [int(x) for x in re.findall(r'data-asset="(\d+)"', html)]


def _get(shop, **params):
    return shop.client.get(reverse("publishing:media-library"), params).content.decode()


@pytest.mark.parametrize(
    "name, shown",
    [
        ("WhatsApp Image 2026-09-10 at 14.22.01.jpeg", "WhatsApp image · 10 Sep"),
        ("Screenshot 2026-10-02 at 09.14.55.png", "Screenshot · 2 Oct"),
        ("IMG_20260305_101112.jpg", "Photo · 5 Mar"),
        ("01_home (1).png", "01 home (1)"),
        ("Zambia 2035_ Cinematic Reel.mp4", "Zambia 2035 Cinematic Reel"),
        ("WhatsApp Image 2026-13-45 at 1.jpg", "WhatsApp Image 2026-13-45 at 1"),
        ("", "Untitled"),
    ],
)
def test_names_are_cleaned_for_the_grid(name, shown):
    assert media_library.display_name(name) == shown


def test_a_second_upload_of_the_same_file_is_flagged(shop):
    first = shop.asset("01_home.png")
    copy = shop.asset("01_home (1).png", data=b"x" * 10)
    MediaAsset.objects.filter(pk=copy.pk).update(
        size_bytes=first.size_bytes, sha256="", created_at=timezone.now()
    )
    copy.refresh_from_db()
    other = shop.asset("menu.png", data=b"y" * 20)

    found = media_library.duplicates(shop.account, [first, copy, other])

    assert found == {copy.pk: first}
    assert f'data-duplicate-of="{first.pk}"' in _get(shop)


def test_each_card_says_what_remove_will_do(shop):
    free = shop.asset("free.png", data=b"a" * 11)
    published = shop.asset("pub.png", data=b"b" * 12)
    drafted = shop.asset("draft.png", data=b"c" * 13)
    shop.post(published.url, Post.PUBLISHED, Variant.PUBLISHED)
    shop.post(drafted.url, Post.DRAFT)

    html = _get(shop)

    def card(asset):
        start = html.index(f'data-asset="{asset.pk}"')
        return html[start : html.index("</li>", start)]

    assert 'data-confirm-label="Delete"' in card(free)
    assert 'data-confirm-label="Archive"' in card(published)
    # Remove stays in the menu, disabled, with the reason under it.
    blocked = card(drafted)
    assert 'disabled aria-describedby="ml-why-' in blocked
    assert "Used by a draft or scheduled post" in blocked
    assert 'value="remove"' not in blocked


def test_archiving_from_the_selection_bar_can_be_undone(shop):
    from django.contrib.messages import get_messages

    used = shop.asset("used.png", data=b"d" * 14)
    shop.post(used.url, Post.DRAFT)  # archiving is safe even for a file a draft uses
    other = shop.asset("other.png", data=b"e" * 15)
    ids = [used.pk, other.pk]

    resp = shop.client.post(
        reverse("publishing:media-bulk"), {"action": "archive", "ids": ids}
    )

    assert MediaAsset.objects.filter(pk__in=ids, archived_at__isnull=False).count() == 2
    message = next(iter(get_messages(resp.wsgi_request)))
    assert "2 files archived" in str(message)
    undo = next(t for t in message.tags.split() if t.startswith("undo:"))[5:]
    back = shop.client.post(undo, HTTP_HX_REQUEST="true")
    assert back.status_code == 204
    assert MediaAsset.objects.filter(pk__in=ids, archived_at__isnull=True).count() == 2


def test_before_an_upload_the_page_can_ask_if_its_a_copy(shop):
    first = shop.asset("01_home.png")
    url = reverse("publishing:media-similar")

    by_name = shop.client.get(
        url, {"name": "01_home (1).png", "size": first.size_bytes}
    ).json()
    by_hash = shop.client.get(
        url, {"name": "other.png", "size": 1, "sha256": first.sha256}
    ).json()
    different = shop.client.get(url, {"name": "menu.png", "size": 5}).json()
    theirs = (
        Shop("other")
        .client.get(url, {"name": "01_home.png", "size": first.size_bytes})
        .json()
    )

    assert by_name["match"]["id"] == first.pk and by_hash["match"]["id"] == first.pk
    assert different["match"] is None and theirs["match"] is None


def test_show_filters_and_old_links(shop):
    used = shop.asset("used.png")
    unused = shop.asset("unused.png")
    video = MediaAsset.objects.create(
        account=shop.account, url=MEDIA + "v.mp4", url_digest="v", kind="video"
    )
    gone = shop.asset("gone.png")
    gone.archived_at = timezone.now()
    gone.save()
    shop.post(used.url)

    assert set(_ids(_get(shop))) == {used.pk, unused.pk, video.pk}
    assert _ids(_get(shop, show="video")) == [video.pk]
    assert set(_ids(_get(shop, show="unused"))) == {unused.pk, video.pk}
    assert _ids(_get(shop, show="archived")) == [gone.pk]
    # Links from before the filter row (Overview, emails) keep working.
    assert _ids(_get(shop, archived="1")) == [gone.pk]
    assert _ids(_get(shop, kind="video")) == [video.pk]


def test_search_finds_names_and_tags_and_sorting(shop):
    beach = shop.asset("beach.png")
    menu = shop.asset("menu.png")
    media_library.set_tags(menu, "summer")
    shop.post(beach.url)
    shop.post(beach.url, Post.PUBLISHED, Variant.PUBLISHED)

    assert _ids(_get(shop, q="beach")) == [beach.pk]
    assert _ids(_get(shop, q="Summer")) == [menu.pk]
    assert _ids(_get(shop, sort="used"))[0] == beach.pk
    assert _ids(_get(shop, sort="name")) == [beach.pk, menu.pk]
    assert 'data-used="2"' in _get(shop, sort="used")


def test_the_grid_groups_by_date_and_counts_the_library(shop):
    old = shop.asset("old.png")
    MediaAsset.objects.filter(pk=old.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=400)
    )
    shop.asset("new.png")

    html = _get(shop)

    assert "This week" in html and "2 files" in html
    assert "This week" not in _get(shop, view="list")


def test_counting_every_file_takes_a_fixed_number_of_queries(
    shop, django_assert_max_num_queries
):
    for i in range(6):
        shop.post(shop.asset(f"p{i}.png").url)
    with django_assert_max_num_queries(3):
        counts = media_library.usage_counts_all(shop.account)
    assert sorted(counts.values()) == [1] * 6


def test_details_open_as_a_drawer_and_say_what_remove_will_do(shop):
    asset = shop.asset("beach.png")
    shop.post(asset.url, Post.PUBLISHED, Variant.PUBLISHED)
    url = reverse("publishing:media-detail", args=[asset.pk])

    drawer = shop.client.get(url, **OVERLAY)
    page = shop.client.get(url)

    assert drawer[OVERLAY_HEADER] == "1"
    html = drawer.content.decode()
    assert 'data-overlay="drawer"' in html and "<html" not in html
    assert "Archive beach.png?" not in html  # the clean name
    assert "Archive beach?" in html and "Used in 1 post" in html
    assert page.status_code == 200 and "<html" in page.content.decode()


def test_a_file_a_draft_needs_cant_be_removed_from_its_details(shop):
    asset = shop.asset()
    shop.post(asset.url, Post.DRAFT)

    html = shop.client.get(
        reverse("publishing:media-detail", args=[asset.pk]), **OVERLAY
    ).content.decode()

    assert "Can't be removed yet" in html


def test_saving_in_the_drawer_closes_it(shop):
    asset = shop.asset()

    resp = shop.client.post(
        reverse("publishing:media-update", args=[asset.pk]),
        {"action": "tags", "name": "Beach", "tags": "sun"},
        **OVERLAY,
    )

    assert resp.status_code == 204 and "overlay-close" in resp["HX-Trigger"]
    asset.refresh_from_db()
    assert asset.name == "Beach" and asset.tags == ["sun"]


def test_bulk_tag_remove_and_restore(shop):
    free = shop.asset("free.png")
    published = shop.asset("pub.png")
    drafted = shop.asset("draft.png")
    shop.post(published.url, Post.PUBLISHED, Variant.PUBLISHED)
    shop.post(drafted.url, Post.DRAFT)
    theirs = Shop("other").asset()
    url = reverse("publishing:media-bulk")
    ids = [free.pk, published.pk, drafted.pk, theirs.pk]

    shop.client.post(url, {"action": "tag", "ids": ids, "tags": "Sale, #new"})
    resp = shop.client.post(url, {"action": "remove", "ids": ids}, follow=True)

    published.refresh_from_db()
    drafted.refresh_from_db()
    theirs.refresh_from_db()
    assert drafted.tags == ["sale", "new"] and theirs.tags == []
    assert not MediaAsset.objects.filter(pk=free.pk).exists()
    assert published.archived_at is not None and drafted.archived_at is None
    html = resp.content.decode()
    assert "1 file deleted" in html and "1 file kept" in html

    shop.client.post(url, {"action": "restore", "ids": [published.pk]})
    published.refresh_from_db()
    assert published.archived_at is None


def test_use_in_post_starts_a_post_with_the_file(shop):
    asset = shop.asset()
    gone = shop.asset("old.png")
    gone.archived_at = timezone.now()
    gone.save()
    theirs = Shop("other").asset()
    url = reverse("publishing:post-new")

    data = shop.client.get(url, {"media": asset.pk}).context["editor_data"]
    assert data["media_urls"] == [asset.url] and data["media_type"] == "image"
    for other in (gone, theirs):
        data = shop.client.get(url, {"media": other.pk}).context["editor_data"]
        assert data["media_urls"] == []

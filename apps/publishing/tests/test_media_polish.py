"""Media page polish (2026-11-11 audit): small thumbnails and known sizes, clean names,
grid/list without a reload, the file panel's autosave, and the Reel cover tool."""

import io

import pytest
from django.urls import reverse
from PIL import Image

from apps.core.htmx import OVERLAY_TARGET
from apps.publishing import media_library
from apps.publishing.models import MediaAsset
from apps.publishing.tests.test_media_library import MEDIA, Shop

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


def _png(size):
    buf = io.BytesIO()
    Image.new("RGB", size, "blue").save(buf, "PNG")
    return buf.getvalue()


def test_a_thumbnail_is_a_small_webp():
    thumb = media_library.make_thumbnail(_png((2048, 2732)))
    with Image.open(io.BytesIO(thumb)) as img:
        assert img.format == "WEBP"
        assert img.width <= 600 and img.height <= 750


def test_details_fill_a_missing_size_and_thumbnail(shop, monkeypatch):
    data = _png((1200, 1500))
    asset = MediaAsset.objects.create(
        account=shop.account,
        url=f"{MEDIA}publishing/{shop.account.pk}/big.png",
        url_digest="d1",
        name="big.png",
        kind="image",
    )

    class _Resp:
        ok = True
        headers = {"Content-Length": str(len(data))}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            yield data

    monkeypatch.setattr("requests.get", lambda *a, **kw: _Resp())
    monkeypatch.setattr("apps.publishing.services._is_own_media_url", lambda url: True)

    changed = media_library.fill_details(asset)

    asset.refresh_from_db()
    assert set(changed) >= {"size_bytes", "width", "height", "thumb_url"}
    assert asset.size_bytes == len(data)
    assert (asset.width, asset.height) == (1200, 1500)
    assert asset.thumb_url.endswith(".webp")


@pytest.mark.parametrize(
    "name,shown",
    [
        ("zambia_2035_natural_final_K2nNF6v.mp4", "zambia 2035 natural final"),
        ("Storyboard_Sv1jm0R.png", "Storyboard"),
        ("photo_summer1.jpg", "photo summer1"),
        ("ad_Summer1.jpg", "ad Summer1"),
    ],
)
def test_storage_suffixes_are_left_out_of_names(name, shown):
    assert media_library.display_name(name) == shown


def test_the_grid_uses_thumbnails_and_both_layouts_are_on_the_page(shop):
    a = shop.asset("01_home.png")
    MediaAsset.objects.filter(pk=a.pk).update(thumb_url=f"{MEDIA}thumbs/x.webp")

    html = shop.client.get(reverse("publishing:media-library")).content.decode()

    assert f'src="{MEDIA}thumbs/x.webp" alt="01 home"' in html
    assert "x-show=\"view === 'grid'\"" in html and "x-show=\"view === 'list'\"" in html
    assert 'name="view"' not in html  # switching doesn't reload the results
    assert 'data-used="0">Not used yet<' in html  # the badge, not green "Unused"
    assert "ml-wash" not in html


def test_unknown_sizes_mark_the_total(shop):
    shop.asset("a.png")
    MediaAsset.objects.create(
        account=shop.account,
        url=f"{MEDIA}v.mp4",
        url_digest="v",
        name="v.mp4",
        kind="video",
    )

    html = shop.client.get(reverse("publishing:media-library")).content.decode()

    assert "without a known size yet" in html


def test_archived_shows_once(shop):
    html = shop.client.get(
        reverse("publishing:media-library") + "?show=archived"
    ).content.decode()
    assert 'data-chip-clear="show kind archived"' not in html
    assert "still in published posts are kept here" in html


def test_the_panel_saves_as_you_go_and_shows_the_file_name(shop):
    a = shop.asset("02_discover.png")
    url = reverse("publishing:media-update", args=[a.pk])

    html = shop.client.get(url, **OVERLAY).content.decode()
    assert 'value="02 discover"' in html and "File: 02_discover.png" in html
    assert 'hx-trigger="change"' in html

    response = shop.client.post(
        url,
        {
            "action": "tags",
            "autosave": "1",
            "name": "02 discover",
            "tags": "launch, team",
        },
        HTTP_HX_REQUEST="true",
    )
    a.refresh_from_db()
    assert response.status_code == 200 and "X-Toast" in response.headers
    assert (
        a.name == "02_discover.png"
    )  # the display name left as it was keeps the file's
    assert a.tags == ["launch", "team"]


def test_the_reel_cover_shows_the_whole_video(shop):
    a = shop.asset("reel.mp4", kind="video", data=b"\x00" * 10)

    html = shop.client.get(
        reverse("publishing:media-edit", args=[a.pk]), **OVERLAY
    ).content.decode()

    assert 'class="me-stage me-video"' in html
    assert "aspect-video" not in html
    assert '@timeupdate="seconds' in html

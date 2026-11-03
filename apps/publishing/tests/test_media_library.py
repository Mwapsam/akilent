"""Phase 3 (``cs_media_library``): every upload is recorded; removing follows one rule
(blocked / archived / deleted); edits and Reel covers are new files with the original
kept; stock photos keep their credit; caption templates and hashtag sets reach the
composer."""

import io
import subprocess
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.accounts.models import Account, Membership
from apps.publishing import media_library
from apps.publishing.models import (
    CaptionTemplate,
    ContentPost,
    ContentPostVariant,
    HashtagSet,
    MediaAsset,
)

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status
P = ContentPost.Status
MEDIA = "https://media.akilent.test/media/"


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_URL = MEDIA
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({media_library.FLAG})


class Shop:
    def __init__(self, name="shop"):
        self.account = Account.objects.create(company_name=name)
        self.user = User.objects.create_user(name, f"{name}@x.example", "pass")
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        self.client = Client()
        self.client.force_login(self.user)

    def asset(self, name="a.jpg", kind="image", data=None):
        data = data or _png()
        path = default_storage.save(
            f"publishing/{self.account.pk}/{name}", io.BytesIO(data)
        )
        return media_library.record(
            self.account,
            default_storage.url(path),
            kind=kind,
            mime="image/png",
            name=name,
            data=data,
        )

    def post(self, url, status=P.DRAFT, variant_status=S.DRAFT, cover=False):
        post = ContentPost.objects.create(
            account=self.account,
            platforms=["instagram"],
            media_type="reel" if cover else "image",
            media_urls=[] if cover else [url],
            status=status,
        )
        ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=variant_status,
            options={"cover_url": url} if cover else {},
        )
        return post


@pytest.fixture
def shop():
    return Shop()


def _png(size=(40, 20)):
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, "PNG")
    return buf.getvalue()


# --- Recording ---------------------------------------------------------------------------


def test_an_upload_is_recorded_once_released(shop, settings):
    url = reverse("publishing:post-media-upload")
    first = shop.client.post(
        url, {"file": SimpleUploadedFile("a.png", _png(), "image/png")}
    )
    assert first.status_code == 200
    assert not MediaAsset.objects.exists()  # not yet released

    settings.RELEASED_FEATURES = frozenset({media_library.FLAG})
    resp = shop.client.post(
        url, {"file": SimpleUploadedFile("b.png", _png(), "image/png")}
    )

    asset = MediaAsset.objects.get()
    assert asset.url == resp.json()["url"]
    assert (asset.width, asset.height) == (40, 20)
    assert asset.uploaded_by == shop.user and len(asset.sha256) == 64


def test_recording_the_same_url_twice_keeps_one_asset(shop):
    a = shop.asset()
    b = media_library.record(shop.account, a.url, kind="image")

    assert a.pk == b.pk and MediaAsset.objects.count() == 1


def test_a_library_failure_never_fails_the_upload(shop, released):
    with patch.object(media_library, "record", side_effect=RuntimeError("boom")):
        resp = shop.client.post(
            reverse("publishing:post-media-upload"),
            {"file": SimpleUploadedFile("a.png", _png(), "image/png")},
        )

    assert resp.status_code == 200


# --- Removing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,variant_status",
    [
        (P.DRAFT, S.DRAFT),
        (P.SCHEDULED, S.SCHEDULED),
        (P.PUBLISHING, S.PUBLISHING),
        # A published post whose channel is still being checked still needs it.
        (P.PUBLISHED, S.RECONCILING),
    ],
)
def test_a_file_a_live_post_uses_cant_be_removed(shop, status, variant_status):
    asset = shop.asset()
    post = shop.post(asset.url, status, variant_status)

    outcome, posts = media_library.remove(asset)

    assert outcome == "blocked" and posts == [post]
    assert MediaAsset.objects.filter(pk=asset.pk, archived_at__isnull=True).exists()


def test_a_reel_cover_counts_as_use(shop):
    asset = shop.asset()
    shop.post(asset.url, cover=True)

    assert media_library.remove(asset)[0] == "blocked"


def test_a_file_only_published_posts_use_is_archived_and_kept(shop):
    asset = shop.asset()
    shop.post(asset.url, P.PUBLISHED, S.PUBLISHED)
    name = asset.url[len(MEDIA) :]

    outcome, _ = media_library.remove(asset)

    asset.refresh_from_db()
    assert outcome == "archived" and asset.archived_at is not None
    assert default_storage.exists(name)


def test_an_unused_file_is_deleted_with_its_file(shop):
    asset = shop.asset()
    name = asset.url[len(MEDIA) :]

    assert media_library.remove(asset) == ("deleted", [])

    assert not MediaAsset.objects.filter(pk=asset.pk).exists()
    assert not default_storage.exists(name)


def test_another_businesss_post_doesnt_count(shop):
    asset = shop.asset()
    other = Shop("other")
    other.post(asset.url)

    assert media_library.remove(asset)[0] == "deleted"


def test_remove_and_restore_through_the_page(shop, released):
    asset = shop.asset()
    shop.post(asset.url, P.PUBLISHED, S.PUBLISHED)
    url = reverse("publishing:media-update", args=[asset.pk])

    resp = shop.client.post(url, {"action": "remove"}, follow=True)
    assert "Archived" in resp.content.decode()
    shop.client.post(url, {"action": "restore"})

    asset.refresh_from_db()
    assert asset.archived_at is None


# --- The library page --------------------------------------------------------------------


def test_the_library_lists_filters_and_tags(shop, released):
    pic = shop.asset("beach.png")
    other = shop.asset("menu.png")
    shop.post(pic.url)
    shop.client.post(
        reverse("publishing:media-update", args=[pic.pk]),
        {"action": "tags", "tags": "Summer, #Beach, summer", "name": "Beach day"},
    )

    pic.refresh_from_db()
    assert pic.tags == ["summer", "beach"] and pic.name == "Beach day"
    html = shop.client.get(
        reverse("publishing:media-library") + "?tag=summer"
    ).content.decode()
    assert f'data-asset="{pic.pk}"' in html and f'data-asset="{other.pk}"' not in html
    assert 'data-used="1"' in html
    assert 'href="/publishing/media/"' in html  # the Media tab


def test_the_library_is_hidden_until_released(shop):
    assert shop.client.get(reverse("publishing:media-library")).status_code == 404
    assert shop.client.get(reverse("publishing:media-picker")).status_code == 404


def test_another_businesss_asset_is_out_of_reach(shop, released):
    theirs = Shop("other").asset()

    resp = shop.client.post(
        reverse("publishing:media-update", args=[theirs.pk]), {"action": "remove"}
    )

    assert resp.status_code == 404
    assert MediaAsset.objects.filter(pk=theirs.pk).exists()


# --- Editing -----------------------------------------------------------------------------


def test_an_edit_is_a_new_picture_and_the_original_is_kept(shop, released):
    original = shop.asset(data=_png((100, 100)))
    original.tags = ["summer"]
    original.save()

    resp = shop.client.post(
        reverse("publishing:media-edit", args=[original.pk]),
        {"file": SimpleUploadedFile("edited.jpg", _png((80, 100)), "image/jpeg")},
    )

    copy = MediaAsset.objects.get(pk=resp.json()["id"])
    assert copy.parent == original and copy.source == MediaAsset.Source.EDIT
    assert copy.url != original.url and copy.tags == ["summer"]
    assert (copy.width, copy.height) == (80, 100)
    original.refresh_from_db()
    assert original.archived_at is None


def test_an_edit_that_isnt_a_picture_is_refused(shop, released):
    original = shop.asset()

    resp = shop.client.post(
        reverse("publishing:media-edit", args=[original.pk]),
        {"file": SimpleUploadedFile("x.jpg", b"not a picture", "image/jpeg")},
    )

    assert resp.status_code == 400
    assert MediaAsset.objects.count() == 1


def test_a_cover_frame_is_taken_from_our_own_video(shop):
    video = media_library.record(
        shop.account, MEDIA + "publishing/1/clip.mp4", kind="video", name="clip.mp4"
    )
    frame = _png((1080, 1920))

    with (
        patch(
            "apps.publishing.media_library.shutil.which", return_value="/usr/bin/ffmpeg"
        ),
        patch(
            "apps.publishing.media_library.subprocess.run",
            return_value=MagicMock(returncode=0, stdout=frame),
        ) as run,
    ):
        cover = media_library.grab_cover(video, 2.5)

    args = run.call_args.args[0]
    assert args[args.index("-ss") + 1] == "2.50" and video.url in args
    assert cover.parent == video and cover.source == MediaAsset.Source.COVER
    assert (cover.width, cover.height) == (1080, 1920)


def test_a_cover_is_never_taken_from_another_host(shop):
    video = media_library.record(
        shop.account, "https://elsewhere.example/clip.mp4", kind="video"
    )

    with patch("apps.publishing.media_library.subprocess.run") as run:
        with pytest.raises(media_library.LibraryError):
            media_library.grab_cover(video, 1)

    run.assert_not_called()


def test_the_cover_task_reports_a_failure(shop):
    from django.core.cache import cache

    from apps.publishing.tasks import grab_cover_frame

    video = media_library.record(
        shop.account, MEDIA + "publishing/1/clip.mp4", kind="video"
    )
    with (
        patch(
            "apps.publishing.media_library.shutil.which", return_value="/usr/bin/ffmpeg"
        ),
        patch(
            "apps.publishing.media_library.subprocess.run",
            side_effect=subprocess.TimeoutExpired("ffmpeg", 60),
        ),
    ):
        assert grab_cover_frame.run(video.pk, 1.0) == 0

    assert cache.get(media_library.cover_status_key(video))["error"]


# --- Stock photos ------------------------------------------------------------------------


def _pexels_photo(src="https://images.pexels.com/photos/1/a.jpeg?w=940"):
    return {
        "id": 1,
        "url": "https://www.pexels.com/photo/1/",
        "photographer": "Ama Mensah",
        "photographer_url": "https://www.pexels.com/@ama",
        "alt": "Sandals on sand",
        "src": {"large2x": src, "medium": src},
    }


def test_a_stock_photo_is_copied_with_its_credit(shop, settings):
    settings.PEXELS_API_KEY = "key"
    api = MagicMock(status_code=200)
    api.json.return_value = _pexels_photo()
    image = MagicMock(status_code=200, headers={"Content-Type": "image/png"})
    image.iter_content.return_value = [_png()]

    with patch("requests.get", side_effect=[api, image]) as get:
        asset = media_library.stock_import(shop.account, "1", shop.user)

    assert get.call_args_list[0].kwargs["headers"] == {"Authorization": "key"}
    assert asset.source == MediaAsset.Source.STOCK
    assert asset.credit == "Photo by Ama Mensah on Pexels"
    assert asset.credit_url == "https://www.pexels.com/photo/1/"
    assert asset.url.startswith(MEDIA + f"publishing/{shop.account.pk}/stock/")


def test_a_stock_photo_that_isnt_a_picture_is_refused(shop, settings):
    settings.PEXELS_API_KEY = "key"
    api = MagicMock(status_code=200)
    api.json.return_value = _pexels_photo()
    image = MagicMock(status_code=200, headers={"Content-Type": "image/jpeg"})
    image.iter_content.return_value = [b"<html>not a photo</html>"]

    with patch("requests.get", side_effect=[api, image]):
        with pytest.raises(media_library.LibraryError, match="picture we can read"):
            media_library.stock_import(shop.account, "1", shop.user)

    assert not MediaAsset.objects.exists()


def test_a_stock_photo_from_another_host_is_refused(shop, settings):
    settings.PEXELS_API_KEY = "key"
    api = MagicMock(status_code=200)
    api.json.return_value = _pexels_photo(src="https://evil.example/a.jpg")

    with patch("requests.get", side_effect=[api]):
        with pytest.raises(media_library.LibraryError):
            media_library.stock_import(shop.account, "1", shop.user)

    assert not MediaAsset.objects.exists()


def test_stock_is_off_without_a_key(shop, released, settings):
    settings.PEXELS_API_KEY = ""

    resp = shop.client.get(reverse("publishing:media-stock-search") + "?q=sandals")

    assert resp.status_code == 404
    assert (
        "Stock photos"
        not in shop.client.get(reverse("publishing:media-library")).content.decode()
    )


# --- Composer picker, templates and hashtag sets -----------------------------------------


def test_the_picker_lists_live_files_of_the_right_kind(shop, released):
    pic = shop.asset()
    gone = shop.asset("old.png")
    gone.archived_at = timezone.now()
    gone.save()
    media_library.record(shop.account, MEDIA + "publishing/1/v.mp4", kind="video")

    items = shop.client.get(reverse("publishing:media-picker") + "?kind=image").json()[
        "items"
    ]

    assert [i["id"] for i in items] == [pic.pk]


def test_templates_and_hashtag_sets_reach_the_composer(shop, released):
    url = reverse("publishing:media-snippets")
    shop.client.post(
        url, {"action": "template", "name": "Restock", "body": "Back in stock!"}
    )
    shop.client.post(
        url, {"action": "hashtags", "name": "Summer", "tags": "#sun, sand #Sun"}
    )
    too_many = shop.client.post(
        url,
        {
            "action": "hashtags",
            "name": "Many",
            "tags": " ".join(f"#t{i}" for i in range(31)),
        },
        follow=True,
    )

    assert HashtagSet.objects.get(name="Summer").tags == ["sun", "sand"]
    assert "30 hashtags at most" in too_many.content.decode()
    data = shop.client.get(reverse("publishing:post-new")).context["editor_data"]
    assert data["caption_templates"] == [{"name": "Restock", "body": "Back in stock!"}]
    assert data["hashtag_sets"] == [{"name": "Summer", "tags": ["sun", "sand"]}]
    assert data["library"]["picker"] == reverse("publishing:media-picker")


def test_the_composer_has_nothing_extra_until_released(shop):
    CaptionTemplate.objects.create(account=shop.account, name="x", body="y")

    resp = shop.client.get(reverse("publishing:post-new"))

    assert "caption_templates" not in resp.context["editor_data"]
    assert "From library" not in resp.content.decode()


# --- Backfill ----------------------------------------------------------------------------


def test_backfill_adds_existing_post_media_once(shop):
    shop.post(MEDIA + "publishing/1/old.jpg")
    shop.post("https://elsewhere.example/x.jpg")

    out = StringIO()
    call_command("media_library_backfill", stdout=out)
    call_command("media_library_backfill", stdout=StringIO())

    assert "Added 1 file" in out.getvalue()
    assert list(MediaAsset.objects.values_list("url", flat=True)) == [
        MEDIA + "publishing/1/old.jpg"
    ]


def test_names_are_decoded_and_backfill_fixes_old_ones(shop):
    url = MEDIA + "publishing/1/My%20photo.jpg"
    shop.post(url)
    call_command("media_library_backfill", stdout=StringIO())
    assert MediaAsset.objects.get().name == "My photo.jpg"

    MediaAsset.objects.update(name="My%20photo.jpg")  # as an earlier run saved it
    typed = shop.asset("x.png")
    MediaAsset.objects.filter(pk=typed.pk).update(name="50%25 off")  # typed by someone
    out = StringIO()
    call_command("media_library_backfill", stdout=out)

    assert "Fixed the names of 1 file" in out.getvalue()
    assert MediaAsset.objects.get(url=url).name == "My photo.jpg"
    assert MediaAsset.objects.get(pk=typed.pk).name == "50%25 off"


def test_remove_never_redirects_off_site(shop, released):
    asset = shop.asset()
    url = reverse("publishing:media-update", args=[asset.pk])
    for bad in ("//evil.example/x", "https://evil.example/", r"/\evil.example"):
        resp = shop.client.post(url, {"action": "tags", "tags": "a", "next": bad})
        assert resp.url == reverse("publishing:media-library"), bad
    resp = shop.client.post(
        url, {"action": "tags", "tags": "a", "next": "/publishing/media/?tag=a"}
    )
    assert resp.url == "/publishing/media/?tag=a"


def test_the_library_page_counts_usage_in_bulk(shop, released):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    assets = [shop.asset(f"p{i}.png") for i in range(2)]
    shop.post(assets[0].url)
    shop.post(assets[0].url)
    shop.post(assets[1].url, cover=True)
    assert media_library.usage_counts(shop.account, assets) == {
        assets[0].pk: 2,
        assets[1].pk: 1,
    }
    url = reverse("publishing:media-library")
    shop.client.get(url)  # warm caches (session, release flags)
    with CaptureQueriesContext(connection) as few:
        html = shop.client.get(url).content.decode()
    assert 'data-used="2"' in html
    for i in range(10):
        shop.asset(f"q{i}.png")
    with CaptureQueriesContext(connection) as many:
        shop.client.get(url)
    # Not one query per asset any more.
    assert len(many) - len(few) <= 2, [q["sql"][:80] for q in many.captured_queries]

"""Phase 2b (``cs_csv_import``): a CSV previews per row and imports as drafts only;
media from outside comes in only through the SSRF guard."""

import io
import socket
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from PIL import Image

from apps.accounts.models import Account, BusinessHours, Membership
from apps.publishing import csv_import
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
)

pytestmark = pytest.mark.django_db

MEDIA = "https://media.akilent.test/media/"
OWN = MEDIA + "publishing/1/a.jpg"
HEADER = "caption,platforms,media_type,media_urls,scheduled_at,hashtags\n"


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_URL = MEDIA
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({csv_import.FLAG})


class Shop:
    def __init__(self, name="shop"):
        self.account = Account.objects.create(company_name=name)
        BusinessHours.objects.create(account=self.account, timezone="Europe/London")
        self.user = User.objects.create_user(name, f"{name}@x.example", "pass")
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        FacebookPageConnection.objects.create(
            account=self.account,
            page_id=f"p_{name}",
            page_name="Page",
            access_token="t",
        )
        self.client = Client()
        self.client.force_login(self.user)


@pytest.fixture
def shop():
    return Shop()


def _file(text: str, name="posts.csv"):
    return SimpleUploadedFile(name, text.encode("utf-8"), content_type="text/csv")


def _public_dns(host, port, *args, **kwargs):
    ip = "10.0.0.5" if "internal" in host else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


# --- Reading the file ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "content,error",
    [
        ("", "empty"),
        ("hello,world\n1,2\n", "first row must name the columns"),
        (HEADER, "no posts"),
        (HEADER + "Hi,facebook,text,,,\n" * (csv_import.MAX_ROWS + 1), "up to 100"),
    ],
)
def test_files_that_cant_be_read(content, error):
    with pytest.raises(csv_import.CsvError, match=error):
        csv_import.read_file(_file(content))


def test_a_file_over_1_mb_is_refused():
    big = _file(HEADER + "x" * (csv_import.MAX_BYTES + 1))
    with pytest.raises(csv_import.CsvError, match="over 1 MB"):
        csv_import.read_file(big)


def test_a_file_that_isnt_utf8_is_refused():
    upload = SimpleUploadedFile("p.csv", HEADER.encode() + b"\xff\xfe,\n")
    with pytest.raises(csv_import.CsvError, match="UTF-8"):
        csv_import.read_file(upload)


# --- Preview -------------------------------------------------------------------------------


def _preview(shop, body):
    with patch("socket.getaddrinfo", side_effect=_public_dns):
        return csv_import.preview(
            shop.account, csv_import.read_file(_file(HEADER + body))
        )


def test_each_row_says_whats_wrong(shop):
    rows = _preview(
        shop,
        "Good,facebook,text,,,\n"
        "Bad channel,tiktok,text,,,\n"
        "Bad type,facebook,gif,,,\n"
        "Bad time,facebook,text,,tomorrow,\n"
        "Past,facebook,text,,2000-01-01 09:00,\n"
        f"Own,instagram,image,{OWN},,\n"
        "Plain http,instagram,image,http://example.com/a.jpg,,\n"
        "External video,instagram,reel,https://example.com/a.mp4,,\n"
        "Internal,instagram,image,https://internal.example/a.jpg,,\n"
        "Public,instagram,image,https://example.com/a.jpg,,\n",
    )

    by_caption = {r["caption"]: r for r in rows}
    assert by_caption["Good"]["ok"]
    assert "Unknown channel: tiktok." in by_caption["Bad channel"]["errors"]
    assert "Unknown media type: gif." in by_caption["Bad type"]["errors"]
    assert "YYYY-MM-DD HH:MM" in by_caption["Bad time"]["errors"][0]
    assert "has passed" in by_caption["Past"]["errors"][0]
    assert by_caption["Own"]["ok"]
    assert "https" in by_caption["Plain http"]["errors"][0]
    assert "Upload videos in the composer" in by_caption["External video"]["errors"][0]
    assert "isn't a public website" in by_caption["Internal"]["errors"][0]
    assert by_caption["Public"]["ok"]


def test_preview_lists_what_to_fix_before_scheduling(shop):
    (row,) = _preview(shop, ",facebook,text,,,\n")  # a text post with no words

    assert row["ok"] and row["to_fix"]


# --- Importing ---------------------------------------------------------------------------


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(buf, "PNG")
    return buf.getvalue()


def _response(body, content_type="image/png", status=200):
    resp = MagicMock(status_code=status, headers={"Content-Type": content_type})
    resp.iter_content.return_value = [body]
    return resp


def test_rows_become_drafts_only_even_with_a_time(shop):
    rows = csv_import.read_file(
        _file(HEADER + "Hello,facebook,text,,2099-05-01 09:00,#sale #new\n")
    )

    result = csv_import.import_rows(shop.account, shop.user, rows)

    (post,) = result["created"]
    assert post.status == ContentPost.Status.DRAFT
    assert post.hashtags == ["sale", "new"]
    assert post.scheduled_at is not None  # kept as the suggested time
    assert not post.variants.filter(status=ContentPostVariant.Status.SCHEDULED).exists()


def test_a_public_image_is_copied_in_through_the_guard(shop):
    rows = csv_import.read_file(
        _file(HEADER + "Hi,instagram,image,https://example.com/a.png,,\n")
    )

    with (
        patch("apps.ai.knowledge._public_ip", return_value="93.184.216.34") as guard,
        patch("apps.ai.knowledge._open", return_value=_response(_png())) as opened,
    ):
        result = csv_import.import_rows(shop.account, shop.user, rows)

    (post,) = result["created"]
    # Checked when the row is read, and again for the address the fetch is pinned to.
    assert [c.args for c in guard.call_args_list] == [
        ("https://example.com/a.png",)
    ] * 2
    assert opened.call_args.kwargs["ip"] == "93.184.216.34"
    assert post.media_urls[0].startswith(
        MEDIA + f"publishing/{shop.account.pk}/import/"
    )


@pytest.mark.parametrize(
    "response,error",
    [
        (_response(b"<html>", content_type="text/html"), "isn't a JPEG"),
        (_response(b"x" * (csv_import.MAX_IMAGE_BYTES + 1)), "over 8 MB"),
        (_response(b"", status=404), "answered 404"),
    ],
)
def test_media_that_cant_be_copied_fails_its_row(shop, response, error):
    rows = csv_import.read_file(
        _file(HEADER + "Hi,instagram,image,https://example.com/a.png,,\n")
    )

    with (
        patch("apps.ai.knowledge._public_ip", return_value="93.184.216.34"),
        patch("apps.ai.knowledge._open", return_value=response),
    ):
        result = csv_import.import_rows(shop.account, shop.user, rows)

    assert result["created"] == []
    ((n, why),) = result["failed"]
    assert n == 1 and error in why


def test_an_internal_address_is_never_fetched(shop):
    rows = csv_import.read_file(
        _file(HEADER + "Hi,instagram,image,https://internal.example/a.png,,\n")
    )

    with (
        patch("socket.getaddrinfo", side_effect=_public_dns),
        patch("apps.ai.knowledge._open") as opened,
    ):
        result = csv_import.import_rows(shop.account, shop.user, rows)

    opened.assert_not_called()
    assert result["created"] == [] and "public website" in result["failed"][0][1]


# --- The page ----------------------------------------------------------------------------


def test_preview_then_import_through_the_page(shop, released):
    url = reverse("publishing:csv-import")
    preview = shop.client.post(
        url, {"file": _file(HEADER + "One,facebook,text,,,\nBad,tiktok,text,,,\n")}
    )

    html = preview.content.decode()
    assert "1 of 2 rows can be imported" in html
    assert 'data-import-row="2" data-ok="false"' in html
    assert ContentPost.objects.count() == 0  # nothing saved yet

    shop.client.post(url, {"action": "import"})

    (post,) = ContentPost.objects.filter(account=shop.account)
    assert post.caption == "One" and post.status == ContentPost.Status.DRAFT
    again = shop.client.post(url, {"action": "import"}, follow=True)
    assert "Choose the file again" in again.content.decode()
    assert ContentPost.objects.count() == 1


def test_import_is_hidden_until_released(shop):
    assert shop.client.get(reverse("publishing:csv-import")).status_code == 404


def test_a_media_url_with_commas_stays_one_url(shop):
    url = "https://example.com/image/upload/w_1080,h_1080/p.jpg"
    (row,) = _preview(shop, f'Commas,instagram,image,"{url}",,\n')

    assert row["ok"], row["errors"]
    assert row["media_count"] == 1


def test_importing_the_same_rows_twice_makes_each_draft_once(shop):
    rows = csv_import.read_file(
        _file(HEADER + "One,facebook,text,,,\nTwo,facebook,text,,,\n")
    )

    first = csv_import.import_rows(shop.account, shop.user, rows[:1])  # cut short
    again = csv_import.import_rows(shop.account, shop.user, rows)  # run again in full

    assert len(first["created"]) == 1 and len(again["created"]) == 1
    assert sorted(ContentPost.objects.values_list("caption", flat=True)) == [
        "One",
        "Two",
    ]


def test_a_file_that_isnt_a_picture_fails_its_row_in_plain_words(shop):
    rows = csv_import.read_file(
        _file(HEADER + "Pic,instagram,image,https://example.com/a.jpg,,\n")
    )
    with (
        patch("socket.getaddrinfo", side_effect=_public_dns),
        patch("apps.ai.knowledge._open", return_value=_response(b"not an image")),
    ):
        result = csv_import.import_rows(shop.account, shop.user, rows)

    ((_, why),) = result["failed"]
    assert "isn't a picture we can read" in why


def test_preview_starts_no_media_checks(shop):
    from apps.publishing.models import MediaCheck

    video = MEDIA + "publishing/1/clip.mp4"
    with patch("apps.publishing.media_checks._enqueue") as enqueue:
        _preview(shop, f"Clip,instagram,reel,{video},,\n")

    enqueue.assert_not_called()
    assert not MediaCheck.objects.exists()

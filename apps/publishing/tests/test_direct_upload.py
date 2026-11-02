"""Phase 0c (``cs_direct_upload``): files go straight to S3 through a presigned POST and
are confirmed before they can be used; a Facebook video Meta can't fetch by URL is sent
as bytes instead, once."""

import base64
import hashlib
import time
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing import constraints, direct_upload, media_checks, readiness
from apps.publishing.composer import ComposerInput
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
    MediaCheck,
)
from apps.publishing.providers.facebook import (
    FacebookPublishError,
    FacebookPublishProvider,
)
from apps.publishing.services import publish_variant

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status
HOST = "https://media.akilent.test/"
MEDIA = HOST + "media/"
VID = MEDIA + "publishing/1/abc/clip.mp4"
BUCKET = "akilent-media"
DIGEST = hashlib.sha256(b"video-bytes").digest()
SHA_B64 = base64.b64encode(DIGEST).decode()
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def s3(settings):
    settings.RELEASED_FEATURES = frozenset(
        {direct_upload.FLAG, constraints.FB_FORMAT_FLAG}
    )
    settings.USE_S3_STORAGE = True
    settings.AWS_STORAGE_BUCKET_NAME = BUCKET
    settings.MEDIAFILES_LOCATION = "media"
    settings.MEDIA_URL = MEDIA


@pytest.fixture
def shop():
    account = Account.objects.create(company_name="Upload Co")
    user = User.objects.create_user("owner", "owner@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client


@pytest.fixture
def s3client():
    client = MagicMock()
    client.generate_presigned_post.side_effect = lambda bucket, key, **kw: {
        "url": f"https://{bucket}.s3.amazonaws.com/",
        "fields": {"key": key, **kw["Fields"], "policy": "p", "x-amz-signature": "s"},
    }
    with patch("apps.publishing.direct_upload._client", return_value=client):
        yield client


def _not_found(*args, **kwargs):
    raise ClientError({"Error": {"Code": "404"}}, "HeadObject")


def _start(client, **fields):
    data = {"name": "clip.mp4", "content_type": "video/mp4", "size": 50 * MB}
    data.update(fields)
    return client.post(reverse("publishing:post-media-direct-start"), data)


def _confirm(client, token):
    return client.post(
        reverse("publishing:post-media-direct-confirm"), {"token": token}
    )


def _uploaded(s3client, *, size=50 * MB, content_type="video/mp4", checksum=SHA_B64):
    head = {"ContentLength": size, "ContentType": content_type}
    if checksum:
        head["ChecksumSHA256"] = checksum
    s3client.head_object.side_effect = lambda **kw: (
        head if "/pending/" in kw["Key"] else _not_found()
    )


# --- start: the presigned POST -----------------------------------------------------------


def test_start_pins_key_type_size_checksum_and_expiry(shop, s3client):
    account, client = shop

    resp = _start(client, sha256=SHA_B64, name="../../etc/My Clip!.mp4")

    assert resp.status_code == 200
    body = resp.json()
    _, kwargs = s3client.generate_presigned_post.call_args
    bucket, key = s3client.generate_presigned_post.call_args.args
    assert bucket == BUCKET
    # The server builds the key, under this account's pending prefix.
    assert key.startswith(f"media/pending/publishing/{account.pk}/")
    assert key.endswith("/My_Clip.mp4") and ".." not in key
    assert kwargs["ExpiresIn"] == 600
    conditions = kwargs["Conditions"]
    assert ["content-length-range", 50 * MB, 50 * MB] in conditions
    assert {"Content-Type": "video/mp4"} in conditions
    assert {"x-amz-checksum-sha256": SHA_B64} in conditions
    assert body["fields"]["x-amz-checksum-algorithm"] == "SHA256"
    assert body["token"] and body["url"].startswith("https://")


def test_real_policy_uses_the_regional_endpoint_and_pins_the_key(shop, settings):
    """boto3 itself, offline: no redirect for the browser to follow, and the signed
    policy holds the exact key, size and checksum."""
    import json

    account, _ = shop
    settings.AWS_S3_REGION_NAME = "eu-west-2"
    settings.AWS_ACCESS_KEY_ID = "AKIATEST"
    settings.AWS_SECRET_ACCESS_KEY = "secret"

    upload = direct_upload.start(
        account, name="a.mp4", content_type="video/mp4", size=5, sha256=SHA_B64
    )

    assert upload["url"] == f"https://{BUCKET}.s3.eu-west-2.amazonaws.com/"
    policy = json.loads(base64.b64decode(upload["fields"]["policy"]))
    key = upload["fields"]["key"]
    assert {"key": key} in policy["conditions"]
    assert ["content-length-range", 5, 5] in policy["conditions"]
    assert {"x-amz-checksum-sha256": SHA_B64} in policy["conditions"]


def test_start_without_a_usable_checksum_leaves_it_to_the_server(shop, s3client):
    _, client = shop

    _start(client, sha256="not-a-digest")

    conditions = s3client.generate_presigned_post.call_args.kwargs["Conditions"]
    assert not any(
        "x-amz-checksum-sha256" in c for c in conditions if isinstance(c, dict)
    )


@pytest.mark.parametrize(
    "fields,error",
    [
        ({"content_type": "application/x-msdownload"}, "not a supported media type"),
        ({"content_type": "text/html", "name": "a.mp4"}, "not a supported media type"),
        ({"size": 301 * MB}, "300 MB limit"),
        ({"content_type": "image/png", "size": 101 * MB}, "100 MB limit"),
        ({"size": 0}, "empty"),
    ],
)
def test_start_refuses_wrong_type_and_size(shop, s3client, fields, error):
    _, client = shop

    resp = _start(client, **fields)

    assert resp.status_code == 400
    assert error in resp.json()["error"]
    s3client.generate_presigned_post.assert_not_called()


def test_a_300_mb_reel_is_allowed(shop, s3client):
    assert _start(shop[1], size=300 * MB).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        lambda s: setattr(s, "RELEASED_FEATURES", frozenset()),
        # Needs the Phase 0a media checks to probe what arrives.
        lambda s: setattr(s, "RELEASED_FEATURES", frozenset({direct_upload.FLAG})),
        lambda s: setattr(s, "USE_S3_STORAGE", False),
    ],
)
def test_not_offered_without_flag_checks_or_s3(shop, s3client, settings, change):
    change(settings)
    _, client = shop

    assert _start(client).status_code == 404
    assert _confirm(client, "x").status_code == 404
    s3client.generate_presigned_post.assert_not_called()


def test_composer_is_told_whether_to_upload_directly(shop, s3client, settings):
    _, client = shop
    data = client.get(reverse("publishing:post-new")).context["editor_data"]

    assert data["direct_upload"] is True
    assert data["urls"]["upload_start"] == reverse("publishing:post-media-direct-start")


# --- confirm ---------------------------------------------------------------------------


def test_confirm_moves_the_file_and_records_the_verified_hash(shop, s3client):
    account, client = shop
    token = _start(client, sha256=SHA_B64).json()["token"]
    pending = s3client.generate_presigned_post.call_args.args[1]
    _uploaded(s3client)

    with patch("apps.publishing.media_checks._enqueue") as enqueue:
        resp = _confirm(client, token)

    assert resp.status_code == 200
    final = pending.replace("media/pending/", "media/", 1)
    assert resp.json()["url"] == HOST + final
    copy = s3client.copy_object.call_args.kwargs
    assert copy["Key"] == final and copy["CopySource"]["Key"] == pending
    assert copy["ContentType"] == "video/mp4"
    s3client.delete_object.assert_called_once_with(Bucket=BUCKET, Key=pending)
    check = MediaCheck.objects.get(account=account)
    assert check.sha256 == DIGEST.hex()
    assert check.size_bytes == 50 * MB
    assert check.status == MediaCheck.Status.PENDING  # a video still goes to ffprobe
    enqueue.assert_called_once()


def test_an_image_without_a_browser_hash_is_hashed_by_the_worker(shop, s3client):
    account, client = shop
    token = _start(client, content_type="image/png", size=2 * MB).json()["token"]
    _uploaded(s3client, size=2 * MB, content_type="image/png", checksum="")

    with patch("apps.publishing.media_checks._enqueue") as enqueue:
        assert _confirm(client, token).status_code == 200

    check = MediaCheck.objects.get(account=account)
    assert check.sha256 == "" and check.status == MediaCheck.Status.PENDING
    enqueue.assert_called_once()


def test_an_image_with_a_verified_hash_is_checked_at_once(shop, s3client):
    account, client = shop
    token = _start(
        client, content_type="image/png", size=2 * MB, sha256=SHA_B64
    ).json()["token"]
    _uploaded(s3client, size=2 * MB, content_type="image/png")

    with patch("apps.publishing.media_checks._enqueue") as enqueue:
        _confirm(client, token)

    assert MediaCheck.objects.get(account=account).status == MediaCheck.Status.OK
    enqueue.assert_not_called()


def test_confirm_twice_returns_the_same_file(shop, s3client):
    _, client = shop
    token = _start(client).json()["token"]
    pending = s3client.generate_presigned_post.call_args.args[1]
    _uploaded(s3client)
    with patch("apps.publishing.media_checks._enqueue"):
        first = _confirm(client, token).json()
        # The pending object is gone now; the confirmed one is there.
        s3client.head_object.side_effect = lambda **kw: (
            _not_found()
            if kw["Key"] == pending
            else {"ContentLength": 50 * MB, "ContentType": "video/mp4"}
        )
        second = _confirm(client, token)

    assert second.status_code == 200
    assert second.json()["url"] == first["url"]
    assert s3client.copy_object.call_count == 1


def test_another_accounts_upload_is_refused(shop, s3client):
    _, client = shop
    other = Account.objects.create(company_name="Other Co")
    token = direct_upload.start(other, name="x.mp4", content_type="video/mp4", size=MB)[
        "token"
    ]

    resp = _confirm(client, token)

    assert resp.status_code == 403
    s3client.head_object.assert_not_called()
    s3client.copy_object.assert_not_called()


def test_a_tampered_token_is_refused(shop, s3client):
    _, client = shop
    token = _start(client).json()["token"]

    assert _confirm(client, token[:-2] + "xx").status_code == 403
    s3client.copy_object.assert_not_called()


def test_an_expired_upload_is_refused(shop, s3client):
    _, client = shop
    token = _start(client).json()["token"]
    _uploaded(s3client)
    later = time.time() + direct_upload.TOKEN_MAX_AGE + 1

    with patch("django.core.signing.time.time", return_value=later):
        resp = _confirm(client, token)

    assert resp.status_code == 410
    s3client.copy_object.assert_not_called()


def test_a_missing_upload_is_refused(shop, s3client):
    _, client = shop
    token = _start(client).json()["token"]
    s3client.head_object.side_effect = _not_found

    resp = _confirm(client, token)

    assert resp.status_code == 409
    assert "didn't finish" in resp.json()["error"]
    assert not MediaCheck.objects.exists()


@pytest.mark.parametrize(
    "head",
    [
        {"size": 10 * MB},  # not the size that was started
        {"content_type": "image/png"},  # not the type that was started
        {"checksum": base64.b64encode(b"x" * 32).decode()},  # not the promised bytes
    ],
)
def test_an_object_unlike_what_was_started_is_deleted(shop, s3client, head):
    _, client = shop
    token = _start(client, sha256=SHA_B64).json()["token"]
    pending = s3client.generate_presigned_post.call_args.args[1]
    _uploaded(s3client, **head)

    resp = _confirm(client, token)

    assert resp.status_code == 400
    s3client.copy_object.assert_not_called()
    s3client.delete_object.assert_called_once_with(Bucket=BUCKET, Key=pending)


# --- An upload never confirmed can't be used --------------------------------------------


def test_readiness_blocks_an_unconfirmed_upload(shop):
    account, _ = shop
    url = HOST + f"media/pending/publishing/{account.pk}/abc/a.jpg"
    inp = ComposerInput(
        platforms=["instagram"], media_type="image", media_urls=[url], caption="Hi"
    )

    issues = readiness.check(inp, account, wa_ready=True)

    issue = next(i for i in issues if i["code"] == "media_unconfirmed")
    assert issue["message"] == media_checks.UNCONFIRMED_MESSAGE
    assert set(issue["blocks"]) == set(readiness.GATED_ACTIONS)


def test_the_media_worker_refuses_an_unconfirmed_upload(shop):
    account, _ = shop
    check = MediaCheck.objects.create(
        account=account,
        url=HOST + "media/pending/publishing/1/abc/a.mp4",
        url_digest="d",
        kind="video",
    )

    with patch("apps.publishing.media_checks._ffprobe") as probe:
        media_checks.run(check)

    probe.assert_not_called()
    assert check.status == MediaCheck.Status.FAILED
    assert check.error == media_checks.UNCONFIRMED_MESSAGE


# --- Facebook: bytes when Meta can't fetch the URL --------------------------------------


@pytest.fixture
def page(shop):
    account, _ = shop
    FacebookPageConnection.objects.create(
        account=account, page_id="p1", page_name="Reel Page", access_token="t"
    )
    return account


def _reel(account, url=VID):
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="reel",
        media_urls=[url],
        caption="Hello",
        status=ContentPost.Status.SCHEDULED,
        scheduled_at=timezone.now(),
    )
    return ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        status=S.SCHEDULED,
        scheduled_at=post.scheduled_at,
    )


def _publish(variant, provider):
    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "p1"),
    ):
        return publish_variant(ContentPostVariant.objects.get(pk=variant.pk))


def _fetch_refused():
    return FacebookPublishError(
        "Failed to fetch the file", code=6000, subcode=1363102, http_status=400
    )


def _provider():
    provider = MagicMock()
    provider.start_video_upload.return_value = "v1"
    provider.upload_hosted_video.side_effect = _fetch_refused()
    return provider


def test_a_refused_url_fetch_is_sent_as_bytes_and_published_once(page):
    variant = _reel(page)
    provider = _provider()

    result = _publish(variant, provider)

    provider.upload_video_bytes.assert_called_once_with("v1", VID, 0)
    provider.finish_reel.assert_called_once_with("v1", "Hello")
    assert result["status"] == S.PROCESSING
    variant.refresh_from_db()
    assert variant.metadata["fb_upload"] == "bytes"

    # A retry finds the earlier finish went through: no second upload or finish.
    provider.reset_mock()
    provider.video_status.return_value = {
        "uploading_phase": {"status": "complete"},
        "publishing_phase": {"status": "in_progress"},
    }
    ContentPostVariant.objects.filter(pk=variant.pk).update(status=S.SCHEDULED)
    _publish(variant, provider)

    provider.start_video_upload.assert_not_called()
    provider.upload_hosted_video.assert_not_called()
    provider.upload_video_bytes.assert_not_called()
    provider.finish_reel.assert_not_called()


def test_an_interrupted_byte_upload_resumes_from_metas_offset(page):
    variant = _reel(page)
    variant.metadata = {"fb_video_id": "v1", "fb_kind": "reel", "fb_upload": "bytes"}
    variant.save(update_fields=["metadata"])
    provider = _provider()
    provider.video_status.return_value = {
        "uploading_phase": {"status": "in_progress", "bytes_transfered": 4096},
        "publishing_phase": {"status": "not_started"},
    }

    result = _publish(variant, provider)

    provider.start_video_upload.assert_not_called()
    provider.upload_hosted_video.assert_not_called()
    provider.upload_video_bytes.assert_called_once_with("v1", VID, 4096)
    provider.finish_reel.assert_called_once()
    assert result["status"] == S.PROCESSING


def test_a_failed_byte_upload_reports_its_own_error(page):
    variant = _reel(page)
    provider = _provider()
    provider.upload_video_bytes.side_effect = FacebookPublishError(
        "Video too long", code=6000, subcode=1363128, http_status=400
    )

    result = _publish(variant, provider)

    provider.finish_reel.assert_not_called()
    assert result["status"] == S.FAILED
    assert result["error_type"] == ContentPostVariant.ErrorType.VALIDATION


def test_permission_errors_are_not_retried_as_bytes(page):
    variant = _reel(page)
    provider = _provider()
    provider.upload_hosted_video.side_effect = FacebookPublishError(
        "No permission", code=200, http_status=403
    )

    result = _publish(variant, provider)

    provider.upload_video_bytes.assert_not_called()
    assert result["status"] == S.FAILED


def test_media_from_another_host_is_never_streamed(page):
    variant = _reel(page, url="https://elsewhere.example/clip.mp4")
    provider = _provider()

    result = _publish(variant, provider)

    provider.upload_video_bytes.assert_not_called()
    assert result["status"] == S.FAILED


def test_without_the_flag_a_refused_fetch_fails_as_before(page, settings):
    settings.RELEASED_FEATURES = frozenset({constraints.FB_FORMAT_FLAG})
    variant = _reel(page)
    provider = _provider()

    result = _publish(variant, provider)

    provider.upload_video_bytes.assert_not_called()
    provider.finish_reel.assert_not_called()
    assert result["status"] == S.FAILED


def test_byte_upload_streams_with_offset_and_size():
    src = MagicMock(status_code=206, headers={"Content-Length": "6"})
    src.raw.read.side_effect = [b"abcdef", b""]
    src.__enter__.return_value = src
    sent = MagicMock(status_code=200, content=b'{"success": true}')
    sent.json.return_value = {"success": True}

    with patch("apps.publishing.providers.facebook.requests") as req:
        req.get.return_value = src
        req.post.return_value = sent
        FacebookPublishProvider("p1", "tok").upload_video_bytes("v1", VID, offset=4)

    assert req.get.call_args.kwargs["headers"] == {"Range": "bytes=4-"}
    assert req.get.call_args.kwargs["allow_redirects"] is False
    url = req.post.call_args.args[0]
    headers = req.post.call_args.kwargs["headers"]
    assert url.startswith(
        "https://rupload.facebook.com/video-upload/"
    ) and url.endswith("/v1")
    assert headers["offset"] == "4" and headers["file_size"] == "10"
    assert headers["Authorization"] == "OAuth tok"
    assert len(req.post.call_args.kwargs["data"]) == 6


def test_byte_upload_refuses_a_host_that_ignores_the_range():
    src = MagicMock(status_code=200, headers={"Content-Length": "10"})
    src.__enter__.return_value = src

    with patch("apps.publishing.providers.facebook.requests") as req:
        req.get.return_value = src
        with pytest.raises(RuntimeError):
            FacebookPublishProvider("p1", "tok").upload_video_bytes("v1", VID, offset=4)

    req.post.assert_not_called()


# --- Bucket check ----------------------------------------------------------------------


def test_bucket_check_reports_missing_rules(s3client, settings):
    settings.SITE_URL = "https://app.akilent.test"
    s3client.get_bucket_cors.side_effect = ClientError(
        {"Error": {"Code": "NoSuchCORSConfiguration"}}, "GetBucketCors"
    )
    s3client.get_bucket_lifecycle_configuration.return_value = {"Rules": []}
    out = StringIO()

    call_command("direct_upload_bucket", stdout=out)

    text = out.getvalue()
    assert "CORS POST from https://app.akilent.test: MISSING" in text
    assert "Pending uploads deleted after a day: MISSING" in text
    assert '"media/pending/"' in text
    s3client.put_bucket_cors.assert_not_called()


def test_bucket_check_passes_with_both_rules(s3client, settings):
    settings.SITE_URL = "https://app.akilent.test"
    s3client.get_bucket_cors.return_value = {
        "CORSRules": [
            {"AllowedOrigins": ["https://app.akilent.test"], "AllowedMethods": ["POST"]}
        ]
    }
    s3client.get_bucket_lifecycle_configuration.return_value = {
        "Rules": [
            {
                "Status": "Enabled",
                "Filter": {"Prefix": "media/pending/"},
                "Expiration": {"Days": 1},
            }
        ]
    }
    out = StringIO()

    call_command("direct_upload_bucket", stdout=out)

    assert "Ready for direct uploads." in out.getvalue()

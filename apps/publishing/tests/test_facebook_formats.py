"""Phase 0a (``cs_fb_formats``): Facebook Reels, stories and multi-photo posts on their own
endpoints, Facebook video processing, the daily Reel limit, and the server-side media
checks that readiness enforces."""

import json
import subprocess
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing import constraints, media_checks, readiness
from apps.publishing.api import attention_reasons
from apps.publishing.composer import ComposerInput
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
    MediaCheck,
)
from apps.publishing.providers.facebook import FacebookPublishError
from apps.publishing.services import publish_variant, try_finish_fb_video_variant

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status
E = ContentPostVariant.ErrorType
MEDIA = "https://media.akilent.test/publishing/"
IMG = MEDIA + "a.jpg"
IMG2 = MEDIA + "b.jpg"
VID = MEDIA + "a.mp4"


@pytest.fixture(autouse=True)
def released(settings):
    settings.RELEASED_FEATURES = frozenset({constraints.FB_FORMAT_FLAG})
    settings.MEDIA_URL = MEDIA


@pytest.fixture
def account():
    account = Account.objects.create(company_name="Reel Co")
    FacebookPageConnection.objects.create(
        account=account, page_id="p1", page_name="Reel Page", access_token="t"
    )
    return account


def _post(account, media_type, urls, platforms=("facebook",), caption="Hello"):
    post = ContentPost.objects.create(
        account=account,
        platforms=list(platforms),
        media_type=media_type,
        media_urls=list(urls),
        caption=caption,
        status=ContentPost.Status.SCHEDULED,
        scheduled_at=timezone.now(),
    )
    for p in platforms:
        ContentPostVariant.objects.create(
            post=post, platform=p, status=S.SCHEDULED, scheduled_at=post.scheduled_at
        )
    return post


def _publish(variant, provider):
    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "p1"),
    ):
        return publish_variant(ContentPostVariant.objects.get(pk=variant.pk))


def _poll(variant, provider):
    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "p1"),
    ):
        return try_finish_fb_video_variant(
            ContentPostVariant.objects.get(pk=variant.pk)
        )


def _video_provider(video_id="v1"):
    provider = MagicMock()
    provider.start_video_upload.return_value = video_id
    provider.finish_video_story.return_value = "story_post_1"
    return provider


def _status(publishing="complete", **errors):
    status = {
        "video_status": "ready",
        "uploading_phase": {"status": "complete"},
        "processing_phase": {"status": "complete"},
        "publishing_phase": {"status": publishing},
    }
    for phase, message in errors.items():
        status[f"{phase}_phase"] = {"status": "error", "error": {"message": message}}
    return status


def _meta_error(code, subcode=None, message="refused"):
    return FacebookPublishError(message, code=code, subcode=subcode, http_status=400)


# --- Each format on its own endpoint ---------------------------------------------------


def test_a_reel_is_started_uploaded_finished_then_confirmed(account):
    variant = _post(account, "reel", [VID]).variants.get()
    provider = _video_provider()

    result = _publish(variant, provider)

    provider.start_video_upload.assert_called_once_with("video_reels")
    provider.upload_hosted_video.assert_called_once_with("v1", VID)
    provider.finish_reel.assert_called_once_with("v1", "Hello")
    provider.create_video_post.assert_not_called()
    assert result["status"] == S.PROCESSING
    variant.refresh_from_db()
    assert variant.metadata["fb_video_id"] == "v1"

    provider.video_status.return_value = _status(publishing="in_progress")
    assert not _poll(variant, provider)
    provider.video_status.return_value = _status()
    assert _poll(variant, provider)

    variant.refresh_from_db()
    assert variant.status == S.PUBLISHED
    assert variant.publication_url == "https://www.facebook.com/reel/v1"


def test_a_video_story_is_published_on_the_story_edge(account):
    variant = _post(account, "story", [VID]).variants.get()
    provider = _video_provider()

    _publish(variant, provider)
    provider.video_status.return_value = _status()
    _poll(variant, provider)

    provider.start_video_upload.assert_called_once_with("video_stories")
    provider.create_photo_post.assert_not_called()
    variant.refresh_from_db()
    assert variant.status == S.PUBLISHED
    assert variant.publication_id == "story_post_1"


def test_a_photo_story_uses_an_unpublished_photo(account):
    variant = _post(account, "story", [IMG]).variants.get()
    provider = MagicMock()
    provider.upload_unpublished_photo.return_value = "ph1"
    provider.create_photo_story.return_value = "story_1"

    result = _publish(variant, provider)

    provider.upload_unpublished_photo.assert_called_once_with(IMG)
    provider.create_photo_story.assert_called_once_with("ph1")
    assert result["status"] == S.PUBLISHED


def test_a_carousel_publishes_every_photo_in_one_post(account):
    variant = _post(account, "carousel", [IMG, IMG2]).variants.get()
    provider = MagicMock()
    provider.upload_unpublished_photo.side_effect = ["ph1", "ph2"]
    provider.create_multi_photo_post.return_value = "p1_99"

    result = _publish(variant, provider)

    provider.create_multi_photo_post.assert_called_once_with(["ph1", "ph2"], "Hello")
    provider.create_photo_post.assert_not_called()
    assert result["status"] == S.PUBLISHED


# --- Failure paths -----------------------------------------------------------------------


def test_a_finish_error_from_meta_fails_with_its_type(account):
    variant = _post(account, "reel", [VID]).variants.get()
    provider = _video_provider()
    provider.finish_reel.side_effect = _meta_error(100, subcode=1363128)

    result = _publish(variant, provider)

    assert result["status"] == S.FAILED
    assert result["error_type"] == E.VALIDATION
    variant.refresh_from_db()
    assert variant.metadata["fb_video_id"] == "v1"  # kept, so a retry resumes it


def test_a_1363_code_alone_is_a_validation_error():
    assert _meta_error(6000, subcode=1363129).error_type == E.VALIDATION


def test_an_unknown_finish_outcome_is_reconciled(account):
    variant = _post(account, "reel", [VID]).variants.get()
    provider = _video_provider()
    provider.finish_reel.side_effect = TimeoutError("timed out")

    result = _publish(variant, provider)

    assert result["status"] == S.RECONCILING
    variant.refresh_from_db()
    assert variant.metadata["fb_video_id"] == "v1"


def test_an_upload_timeout_is_retryable_not_reconciled(account):
    variant = _post(account, "reel", [VID]).variants.get()
    provider = _video_provider()
    provider.upload_hosted_video.side_effect = TimeoutError("timed out")

    result = _publish(variant, provider)

    assert result["status"] == S.FAILED and not result["reconciling"]
    provider.finish_reel.assert_not_called()


def test_a_retry_resumes_the_same_video_and_never_starts_another(account):
    variant = _post(account, "reel", [VID]).variants.get()
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        status=S.FAILED, metadata={"fb_video_id": "v1", "fb_kind": "reel"}
    )
    provider = _video_provider()
    provider.video_status.return_value = _status(publishing="not_started")

    result = _publish(variant, provider)

    provider.start_video_upload.assert_not_called()
    provider.upload_hosted_video.assert_not_called()  # already uploaded
    provider.finish_reel.assert_called_once_with("v1", "Hello")
    assert result["status"] == S.PROCESSING


def test_a_retry_after_an_ambiguous_finish_doesnt_publish_twice(account):
    variant = _post(account, "reel", [VID]).variants.get()
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        status=S.FAILED, metadata={"fb_video_id": "v1", "fb_kind": "reel"}
    )
    provider = _video_provider()
    provider.video_status.return_value = _status(publishing="complete")

    result = _publish(variant, provider)

    provider.finish_reel.assert_not_called()
    assert result["status"] == S.PROCESSING  # the poller confirms it


def test_a_processing_error_fails_the_channel(account):
    variant = _post(account, "reel", [VID]).variants.get()
    provider = _video_provider()
    _publish(variant, provider)
    provider.video_status.return_value = _status(
        publishing="not_started", processing="Resolution too low."
    )

    assert not _poll(variant, provider)

    variant.refresh_from_db()
    assert variant.status == S.FAILED
    assert "Resolution too low" in variant.last_error
    assert variant.last_error_type == E.VALIDATION


def test_facebook_video_stuck_in_processing_is_escalated(account):
    from apps.publishing.tasks import escalate_exhausted_processing_variants

    variant = _post(account, "reel", [VID]).variants.get()
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        status=S.PROCESSING,
        processing_started_at=timezone.now() - timedelta(hours=49),
        metadata={"fb_video_id": "v1", "fb_kind": "reel"},
    )

    escalate_exhausted_processing_variants()

    variant.refresh_from_db()
    assert variant.status == S.RECONCILING
    assert variant.metadata["fb_video_id"] == "v1"


def test_an_ambiguous_multi_photo_post_is_reconciled(account):
    variant = _post(account, "carousel", [IMG, IMG2]).variants.get()
    provider = MagicMock()
    provider.upload_unpublished_photo.side_effect = ["ph1", "ph2"]
    provider.create_multi_photo_post.side_effect = ConnectionError("reset")

    assert _publish(variant, provider)["status"] == S.RECONCILING


# --- Facebook's daily Reel limit -------------------------------------------------------


def test_a_rate_limited_reel_is_deferred_not_failed(account):
    from apps.publishing.tasks import publish_variant_async

    variant = _post(account, "reel", [VID]).variants.get()
    provider = _video_provider()
    provider.finish_reel.side_effect = _meta_error(4, message="Too many Reels")

    with (
        patch(
            "apps.publishing.services._fb_provider_for_post",
            return_value=(provider, "p1"),
        ),
        patch.object(publish_variant_async, "retry") as retry,
    ):
        assert publish_variant_async.run(variant.pk) == "deferred"

    retry.assert_not_called()
    variant.refresh_from_db()
    assert variant.status == S.SCHEDULED
    assert variant.scheduled_at > timezone.now() + timedelta(minutes=55)
    assert variant.dispatched_at is None
    assert variant.last_error_type == E.RATE_LIMIT
    assert variant.metadata["fb_reel_deferrals"] == 1
    assert variant.metadata["fb_video_id"] == "v1"


def test_a_reel_stops_waiting_after_a_day_of_tries(account):
    from apps.publishing.services import FB_REEL_MAX_DEFERRALS

    variant = _post(account, "reel", [VID]).variants.get()
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        metadata={"fb_reel_deferrals": FB_REEL_MAX_DEFERRALS}
    )
    provider = _video_provider()
    provider.start_video_upload.side_effect = _meta_error(4)

    assert _publish(variant, provider)["status"] == S.FAILED


# --- Readiness with the media checks -----------------------------------------------------


def _check(account, **fields):
    inp = ComposerInput(
        platforms=["facebook"], media_type="reel", media_urls=[VID], caption="Hi"
    )
    for k, v in fields.items():
        setattr(inp, k, v)
    return readiness.check(inp, account, wa_ready=True)


def _codes(issues):
    return {i["code"] for i in issues}


def _checked(account, url, **values):
    defaults = {
        "kind": "video",
        "status": MediaCheck.Status.OK,
        "sha256": "h-" + url,
        "duration": 30.0,
        "width": 1080,
        "height": 1920,
        "fps": 30.0,
        "size_bytes": 10_000_000,
    }
    defaults.update(values)
    return MediaCheck.objects.create(
        account=account, url=url, url_digest=media_checks.url_digest(url), **defaults
    )


def test_a_video_being_checked_blocks_and_starts_a_check(account):
    issues = _check(account)

    issue = next(i for i in issues if i["code"] == "media_checking")
    assert issue["message"] == readiness.MEDIA_CHECKING
    assert set(issue["blocks"]) == set(readiness.GATED_ACTIONS)
    assert MediaCheck.objects.get().status == MediaCheck.Status.PENDING


def test_a_slow_check_says_so(account):
    check = _checked(account, VID, status=MediaCheck.Status.PENDING)
    MediaCheck.objects.filter(pk=check.pk).update(
        created_at=timezone.now() - timedelta(minutes=4)
    )

    issue = next(i for i in _check(account) if i["code"] == "media_checking")

    assert issue["message"] == readiness.MEDIA_CHECKING_SLOW


def test_a_failed_check_blocks_with_its_reason(account):
    _checked(account, VID, status=MediaCheck.Status.FAILED, error="No video track.")

    issue = next(i for i in _check(account) if i["code"] == "media_check_failed")

    assert "No video track." in issue["message"]


def test_a_two_minute_clip_is_too_long_for_a_facebook_reel_only(account):
    _checked(account, VID, duration=120.0)

    issues = _check(account, platforms=["facebook", "instagram"])

    limits_issues = [i for i in issues if i["code"] == "video_limits"]
    assert [i["platform"] for i in limits_issues] == ["facebook"]
    assert "at most 90 seconds long" in limits_issues[0]["message"]


def test_a_landscape_video_is_refused_as_a_facebook_reel(account):
    _checked(account, VID, width=1920, height=1080)

    issue = next(i for i in _check(account) if i["code"] == "video_limits")

    assert "vertical 9:16" in issue["message"]


def test_a_facebook_story_cant_reuse_published_media(account):
    old = _post(account, "image", [IMG])
    old.variants.update(status=S.PUBLISHED, published_at=timezone.now())
    _checked(account, IMG, kind="image", sha256="same")
    _checked(account, IMG2, kind="image", sha256="same")

    issues = _check(account, media_type="story", media_urls=[IMG2])

    assert "fb_story_reused" in _codes(issues)
    fresh = _check(account, media_type="story", media_urls=[MEDIA + "new.jpg"])
    assert "fb_story_reused" not in _codes(fresh)  # its check is still running


def test_the_thirtieth_reel_of_the_day_warns(account):
    _checked(account, VID)
    for _ in range(constraints.FB_REELS_PER_DAY):
        post = _post(account, "reel", [VID])
        post.variants.update(status=S.PUBLISHED, last_attempt_at=timezone.now())

    issue = next(i for i in _check(account) if i["code"] == "fb_reel_daily_limit")

    assert issue["blocks"] == []


def test_unreleased_readiness_runs_no_media_checks(account, settings):
    settings.RELEASED_FEATURES = frozenset()

    issues = _check(account)

    assert "media_checking" not in _codes(issues)
    assert not MediaCheck.objects.exists()


# --- Running a check -----------------------------------------------------------------------

FFPROBE = {
    "streams": [
        {
            "codec_type": "video",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "duration": "12.5",
            "side_data_list": [{"rotation": -90}],
        }
    ],
    "format": {"duration": "12.5", "size": "4000000"},
}


def test_a_probe_reads_duration_size_and_rotated_dimensions(account):
    check = _checked(account, VID, status=MediaCheck.Status.PENDING, duration=None)

    with patch("apps.publishing.media_checks._ffprobe", return_value=FFPROBE):
        media_checks.run(check)

    check.refresh_from_db()
    assert check.status == MediaCheck.Status.OK
    assert (check.width, check.height) == (1080, 1920)  # rotated phone video
    assert check.duration == 12.5 and round(check.fps, 2) == 29.97
    assert check.size_bytes == 4_000_000


def test_a_file_on_another_host_is_never_probed(account):
    url = "https://elsewhere.example/a.mp4"
    check = _checked(account, url, status=MediaCheck.Status.PENDING)

    with patch("apps.publishing.media_checks._ffprobe") as probe:
        media_checks.run(check)

    probe.assert_not_called()
    assert check.status == MediaCheck.Status.FAILED
    assert "Upload this file to Akilent" in check.error


def test_a_probe_timeout_fails_the_check(account):
    check = _checked(account, VID, status=MediaCheck.Status.PENDING)

    with patch(
        "apps.publishing.media_checks._ffprobe",
        side_effect=subprocess.TimeoutExpired("ffprobe", 120),
    ):
        media_checks.run(check)

    assert check.status == MediaCheck.Status.FAILED
    assert "took too long" in check.error


def test_a_file_without_a_hash_is_stream_hashed(account):
    check = _checked(account, VID, status=MediaCheck.Status.PENDING, sha256="")

    with (
        patch("apps.publishing.media_checks._ffprobe", return_value=FFPROBE),
        patch("apps.publishing.media_checks._stream_hash", return_value="abc") as h,
    ):
        media_checks.run(check)

    h.assert_called_once_with(VID)
    assert check.sha256 == "abc"


def test_an_uploaded_video_is_hashed_and_queued_for_the_media_worker(
    account, django_capture_on_commit_callbacks
):
    with (
        patch("apps.publishing.tasks.probe_media.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        video = media_checks.record_upload(account, VID, b"video-bytes", "video/mp4")
        image = media_checks.record_upload(account, IMG, b"image-bytes", "image/png")

    delay.assert_called_once_with(video.pk)
    assert video.status == MediaCheck.Status.PENDING and video.sha256
    assert image.status == MediaCheck.Status.OK and image.sha256


def test_probe_runs_on_its_own_queue():
    from apps.publishing.tasks import probe_media

    assert probe_media.queue == "media"


# --- A backlog is visible -------------------------------------------------------------------


def test_a_stuck_check_alerts_owners_once_and_shows_operators(account):
    from django.core.cache import cache

    from apps.publishing.tasks import alert_media_check_backlog

    cache.clear()
    check = _checked(account, VID, status=MediaCheck.Status.PENDING)
    MediaCheck.objects.filter(pk=check.pk).update(
        created_at=timezone.now() - timedelta(minutes=11)
    )

    with patch("apps.accounts.notifications.notify_team") as notify:
        assert alert_media_check_backlog() == 1
        assert alert_media_check_backlog() == 0  # once a day per business

    assert notify.call_args.kwargs["to"] == "owners"
    assert any("media check" in r for r in attention_reasons(account))


def test_a_ffprobe_json_without_video_fails(account):
    check = _checked(account, VID, status=MediaCheck.Status.PENDING)
    audio_only = json.loads(json.dumps({"streams": [{"codec_type": "audio"}]}))

    with patch("apps.publishing.media_checks._ffprobe", return_value=audio_only):
        media_checks.run(check)

    assert check.status == MediaCheck.Status.FAILED
    assert "no video track" in check.error


# --- Review fixes: a dead video and the deferral count don't outlive a failure -----------


def test_a_video_meta_failed_is_forgotten_so_a_retry_uploads_afresh(account):
    variant = _post(account, "reel", [VID]).variants.get()
    provider = _video_provider()
    _publish(variant, provider)
    provider.video_status.return_value = _status(
        publishing="not_started", processing="Bad file."
    )
    _poll(variant, provider)
    variant.refresh_from_db()
    assert variant.status == S.FAILED
    assert "fb_video_id" not in variant.metadata

    retry = _video_provider("v2")
    _publish(variant, retry)
    retry.start_video_upload.assert_called_once()
    retry.finish_reel.assert_called_once_with("v2", "Hello")


def test_a_retry_of_a_video_meta_reports_failed_starts_a_new_one(account):
    variant = _post(account, "reel", [VID]).variants.get()
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        status=S.FAILED, metadata={"fb_video_id": "v1", "fb_kind": "reel"}
    )
    provider = _video_provider("v2")
    provider.video_status.return_value = _status(
        publishing="not_started", processing="Bad file."
    )

    _publish(variant, provider)

    provider.start_video_upload.assert_called_once()
    provider.finish_reel.assert_called_once_with("v2", "Hello")


def test_a_reel_that_used_its_deferrals_waits_again_after_a_manual_retry(account):
    from apps.publishing.services import FB_REEL_MAX_DEFERRALS

    variant = _post(account, "reel", [VID]).variants.get()
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        metadata={"fb_reel_deferrals": FB_REEL_MAX_DEFERRALS}
    )
    provider = _video_provider()
    provider.start_video_upload.side_effect = _meta_error(4)
    assert _publish(variant, provider)["status"] == S.FAILED
    variant.refresh_from_db()
    assert "fb_reel_deferrals" not in variant.metadata

    ContentPostVariant.objects.filter(pk=variant.pk).update(status=S.SCHEDULED)
    assert _publish(variant, provider)["status"] == S.SCHEDULED  # deferred again


def test_a_failed_check_is_tried_again_after_a_while(account):
    check = _checked(account, VID, status=MediaCheck.Status.FAILED, error="Timed out.")
    MediaCheck.objects.filter(pk=check.pk).update(checked_at=timezone.now())
    assert "media_check_failed" in _codes(_check(account))

    MediaCheck.objects.filter(pk=check.pk).update(
        checked_at=timezone.now() - media_checks.RECHECK_AFTER - timedelta(seconds=1)
    )
    with patch("apps.publishing.media_checks._enqueue") as enqueue:
        issues = _check(account)

    assert "media_checking" in _codes(issues)
    enqueue.assert_called_once()
    assert MediaCheck.objects.get(pk=check.pk).status == MediaCheck.Status.PENDING


def test_story_reuse_is_found_beyond_the_newest_published_posts(account):
    old = _post(account, "image", [IMG])
    old.variants.update(
        status=S.PUBLISHED, published_at=timezone.now() - timedelta(days=400)
    )
    for i in range(3):
        newer = _post(account, "image", [MEDIA + f"n{i}.jpg"])
        newer.variants.update(status=S.PUBLISHED, published_at=timezone.now())
    _checked(account, IMG, kind="image", sha256="same")
    _checked(account, IMG2, kind="image", sha256="same")

    assert "fb_story_reused" in _codes(
        _check(account, media_type="story", media_urls=[IMG2])
    )


def test_a_deferred_reel_that_goes_through_forgets_its_deferrals(account):
    variant = _post(account, "reel", [VID]).variants.get()
    ContentPostVariant.objects.filter(pk=variant.pk).update(
        metadata={"fb_reel_deferrals": 3}
    )

    assert _publish(variant, _video_provider())["status"] == S.PROCESSING

    variant.refresh_from_db()
    assert "fb_reel_deferrals" not in variant.metadata
    assert variant.metadata["fb_video_id"] == "v1"


def test_a_rechecked_file_isnt_slow_the_moment_its_queued(account):
    check = _checked(account, VID, status=MediaCheck.Status.FAILED, error="Timed out.")
    old = timezone.now() - timedelta(hours=1)
    MediaCheck.objects.filter(pk=check.pk).update(created_at=old, checked_at=old)

    with patch("apps.publishing.media_checks.probe_media", create=True):
        issue = next(i for i in _check(account) if i["code"] == "media_checking")

    assert issue["message"] == readiness.MEDIA_CHECKING
    assert not media_checks.backlog().filter(pk=check.pk).exists()

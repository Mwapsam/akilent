"""Facebook stories and carousels are blocked until ``cs_fb_formats`` is released.

Facebook would publish a story as a regular post and a carousel as its first image only,
so readiness blocks them, and dispatch fails a channel that was scheduled before the block
existed. The Instagram channel of the same post is unaffected, and every other format
publishes as before.
"""

from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing import constraints, readiness
from apps.publishing.composer import ComposerInput
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
)
from apps.publishing.services import publish_variant

pytestmark = pytest.mark.django_db

IMG = "https://example.com/a.jpg"
VID = "https://example.com/a.mp4"
S = ContentPostVariant.Status


@pytest.fixture
def account():
    account = Account.objects.create(company_name="Story Co")
    FacebookPageConnection.objects.create(
        account=account, page_id="p1", page_name="Story Page", access_token="t"
    )
    return account


@pytest.fixture
def released(settings):
    settings.RELEASED_FEATURES = frozenset({constraints.FB_FORMAT_FLAG})


def _check(account, **fields):
    inp = ComposerInput(
        platforms=["facebook"], media_type="image", media_urls=[IMG], caption="Hi"
    )
    for k, v in fields.items():
        setattr(inp, k, v)
    return readiness.check(inp, account, wa_ready=True)


def _issue(issues, code):
    return next((i for i in issues if i["code"] == code), None)


def _ok(pub_id):
    return {
        "success": True,
        "publication_id": pub_id,
        "publication_url": f"https://example.com/{pub_id}",
        "error": "",
        "error_type": "",
        "error_at": None,
        "reconciling": False,
    }


def _post(account, media_type, platforms=("facebook", "instagram"), urls=(IMG,)):
    post = ContentPost.objects.create(
        account=account,
        platforms=list(platforms),
        media_type=media_type,
        media_urls=list(urls),
        status=ContentPost.Status.SCHEDULED,
        scheduled_at=timezone.now(),
    )
    for p in platforms:
        ContentPostVariant.objects.create(
            post=post, platform=p, status=S.SCHEDULED, scheduled_at=post.scheduled_at
        )
    return post


# --- Readiness ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "media_type,urls", [("story", [IMG]), ("carousel", [IMG, IMG])]
)
def test_facebook_story_and_carousel_are_blocked(account, media_type, urls):
    issues = _check(account, media_type=media_type, media_urls=urls)

    issue = _issue(issues, "fb_format_unsupported")
    assert issue["message"] == constraints.FB_UNSUPPORTED_MESSAGE
    assert issue["platform"] == "facebook"
    assert set(issue["blocks"]) == set(readiness.GATED_ACTIONS)
    # The old "publishes wrongly" warnings give way to the block.
    assert not {"fb_story", "fb_first_image_only"} & {i["code"] for i in issues}


@pytest.mark.parametrize(
    "media_type,urls", [("story", [IMG]), ("carousel", [IMG, IMG])]
)
def test_instagram_only_story_and_carousel_are_not_blocked(account, media_type, urls):
    issues = _check(
        account, platforms=["instagram"], media_type=media_type, media_urls=urls
    )

    assert _issue(issues, "fb_format_unsupported") is None


@pytest.mark.parametrize(
    "media_type,urls", [("story", [IMG]), ("carousel", [IMG, IMG])]
)
def test_released_flag_lifts_the_block(account, released, media_type, urls):
    """Released, Phase 0a publishes these on their own Facebook endpoints."""
    issues = _check(account, media_type=media_type, media_urls=urls)

    codes = {i["code"] for i in issues}
    assert "fb_format_unsupported" not in codes
    assert not {"fb_story", "fb_first_image_only"} & codes


def test_facebook_reel_warns_it_is_a_regular_video(account):
    issues = _check(account, media_type="reel", media_urls=[VID])

    issue = _issue(issues, "fb_reel_as_video")
    assert issue["message"] == constraints.FB_REEL_AS_VIDEO_MESSAGE
    assert issue["level"] == "warning" and issue["blocks"] == []


def test_released_facebook_reel_is_a_real_reel(account, released):
    issues = _check(account, media_type="reel", media_urls=[VID])

    assert _issue(issues, "fb_reel_as_video") is None


# --- Dispatch ----------------------------------------------------------------------


@pytest.mark.parametrize("media_type", ["story", "carousel"])
@patch("apps.publishing.services._publish_variant_instagram")
def test_scheduled_facebook_channel_fails_while_instagram_publishes(
    mock_ig, account, media_type
):
    post = _post(account, media_type, urls=[IMG, IMG])
    mock_ig.return_value = _ok("ig_1")
    provider = MagicMock()

    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "p1"),
    ):
        fb = publish_variant(post.variants.get(platform="facebook"))
        ig = publish_variant(post.variants.get(platform="instagram"))

    assert provider.method_calls == []  # nothing reached Meta for Facebook
    assert fb["status"] == S.FAILED
    assert fb["error"] == constraints.FB_UNSUPPORTED_MESSAGE
    assert fb["error_type"] == ContentPostVariant.ErrorType.VALIDATION
    assert not fb["reconciling"]
    assert ig["status"] == S.PUBLISHED


def test_the_scheduled_task_fails_the_channel_without_retrying(account):
    from apps.publishing.tasks import publish_variant_async

    post = _post(account, "story", platforms=("facebook",))
    fb = post.variants.get()

    with patch.object(publish_variant_async, "retry") as retry:
        publish_variant_async.run(fb.pk, scheduled=True)

    retry.assert_not_called()
    fb.refresh_from_db()
    assert fb.status == S.FAILED
    assert fb.last_error_type == ContentPostVariant.ErrorType.VALIDATION


@pytest.mark.parametrize(
    "media_type,urls,call",
    [
        ("image", [IMG], "create_photo_post"),
        ("video", [VID], "create_video_post"),
        ("reel", [VID], "create_video_post"),
    ],
)
def test_other_formats_publish_as_before(account, media_type, urls, call):
    post = _post(account, media_type, platforms=("facebook",), urls=urls)
    provider = MagicMock()
    getattr(provider, call).return_value = "fb_1"

    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "p1"),
    ):
        result = publish_variant(post.variants.get())

    getattr(provider, call).assert_called_once()
    assert result["status"] == S.PUBLISHED


def test_released_flag_publishes_a_real_facebook_story(account, released):
    """With the flag on, a photo story goes to Facebook's story endpoint (Phase 0a)."""
    post = _post(account, "story", platforms=("facebook",))
    provider = MagicMock()
    provider.upload_unpublished_photo.return_value = "photo_1"
    provider.create_photo_story.return_value = "story_1"

    with patch(
        "apps.publishing.services._fb_provider_for_post",
        return_value=(provider, "p1"),
    ):
        result = publish_variant(post.variants.get())

    provider.create_photo_post.assert_not_called()
    provider.create_photo_story.assert_called_once_with("photo_1")
    assert result["status"] == S.PUBLISHED


# --- publishing_audit --------------------------------------------------------------


def _audit() -> str:
    out = StringIO()
    call_command("publishing_audit", stdout=out)  # no SystemExit: not a deploy blocker
    return out.getvalue()


def test_audit_lists_facebook_formats_waiting_to_publish(account):
    story = _post(account, "story")
    _post(account, "image")
    published = _post(account, "carousel", platforms=("facebook",))
    published.variants.update(status=S.PUBLISHED)

    out = _audit()

    assert "Facebook stories/carousels waiting to publish" in out
    assert "will fail: format not yet supported on Facebook): 1" in out
    assert f"post={story.pk} " in out
    assert f"post={published.pk} " not in out


def test_audit_section_is_absent_once_released(account, released):
    _post(account, "story")

    assert "Facebook stories/carousels" not in _audit()

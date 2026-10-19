"""Channel options: alt text, location, collaborators, Reel settings, first comment.

Each option is kept and sent only for the channels and media types Meta accepts it
for. The first comment is posted once, after the channel goes live.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    FacebookPageConnection,
)
from apps.publishing.post_options import (
    OptionsError,
    instagram_container_params,
    parse_collaborators,
    parse_options,
)
from apps.publishing.providers.errors import MetaPublishError
from apps.publishing.services import (
    FIRST_COMMENT_KEY,
    _publish_variant_instagram,
    post_first_comment,
    queue_first_comments,
    refresh_parent_post,
)

pytestmark = pytest.mark.django_db


class _Form(dict):
    """A QueryDict stand-in with getlist."""

    def getlist(self, key):
        value = self.get(key)
        if value is None:
            return []
        return value if isinstance(value, list) else [value]


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Mwamba Kitchen", slug="mwamba")
    user = User.objects.create_user("owner", "owner@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client, user


def _save(client, **fields):
    data = {
        "caption": "Weekend sale",
        "platforms": ["facebook", "instagram"],
        "media_type": "image",
        "action": "draft",
        **fields,
    }
    return client.post(reverse("publishing:post-new"), data)


# --- Parsing ---------------------------------------------------------------------


def test_options_are_kept_only_where_meta_accepts_them():
    form = _Form(
        ig_alt_text="Three dresses",
        ig_location_id="123",
        ig_location_name="Lusaka",
        ig_collaborators="@ana, bo.b",
        ig_share_to_feed=["0", "1"],
        ig_cover_url="https://cdn.example.com/c.jpg",
        ig_thumb_offset="2.5",
        ig_first_comment="#sale",
        fb_first_comment="Sizes in the photo",
    )

    image = parse_options(form, ["instagram", "facebook"], "image")
    assert image == {
        "instagram": {
            "alt_text": "Three dresses",
            "location": {"id": "123", "name": "Lusaka"},
            "collaborators": ["ana", "bo.b"],
            "first_comment": "#sale",
        },
        "facebook": {"first_comment": "Sizes in the photo"},
    }

    reel = parse_options(form, ["instagram"], "reel")["instagram"]
    assert "alt_text" not in reel  # Meta: images only
    assert reel["share_to_feed"] is True
    assert reel["cover_url"] == "https://cdn.example.com/c.jpg"
    assert reel["thumb_offset"] == 2500

    # Stories take no location, collaborators or comments.
    assert parse_options(form, ["instagram", "facebook"], "story") == {
        "instagram": {},
        "facebook": {},
    }


def test_share_to_feed_off_and_missing():
    off = parse_options(_Form(ig_share_to_feed=["0"]), ["instagram"], "reel")
    assert off["instagram"]["share_to_feed"] is False
    # An older form without the field keeps Instagram's default.
    assert parse_options(_Form(), ["instagram"], "reel") == {"instagram": {}}


@pytest.mark.parametrize(
    "form,media",
    [
        (_Form(ig_collaborators="a b c d"), "image"),
        (_Form(ig_collaborators="not a/name"), "image"),
        (_Form(ig_location_id="abc"), "image"),
        (_Form(ig_alt_text="x" * 1001), "image"),
        (_Form(ig_first_comment="x" * 2201), "image"),
        (_Form(ig_thumb_offset="ten"), "reel"),
        (_Form(ig_thumb_offset="-1"), "reel"),
        (_Form(ig_cover_url="http://x.example.com/c.jpg"), "reel"),
    ],
)
def test_bad_options_are_refused(form, media):
    with pytest.raises(OptionsError):
        parse_options(form, ["instagram"], media)


def test_collaborators_are_deduplicated_ignoring_case():
    assert parse_collaborators("@Ana ana, @bob") == ["Ana", "bob"]


def test_container_params_only_for_the_media_type():
    opts = {
        "alt_text": "A",
        "location": {"id": "9", "name": "X"},
        "collaborators": ["ana"],
        "share_to_feed": False,
        "cover_url": "https://c.example.com/c.jpg",
        "thumb_offset": 1000,
    }
    assert instagram_container_params(opts, "image") == {
        "alt_text": "A",
        "location_id": "9",
        "collaborators": json.dumps(["ana"]),
    }
    assert instagram_container_params(opts, "reel") == {
        "location_id": "9",
        "collaborators": json.dumps(["ana"]),
        "share_to_feed": "false",
        "cover_url": "https://c.example.com/c.jpg",
        "thumb_offset": 1000,
    }
    assert instagram_container_params(opts, "story") == {}


# --- Composer --------------------------------------------------------------------


def test_composer_saves_and_reloads_options(owner):
    account, client, _ = owner
    _save(
        client,
        ig_alt_text="Three dresses",
        ig_collaborators="@ana",
        fb_first_comment="Sizes in the photo",
    )
    post = ContentPost.objects.get(account=account)
    ig = post.variants.get(platform="instagram")
    fb = post.variants.get(platform="facebook")
    assert ig.options == {"alt_text": "Three dresses", "collaborators": ["ana"]}
    assert fb.options == {"first_comment": "Sizes in the photo"}

    resp = client.get(reverse("publishing:post-new") + f"?post_id={post.pk}")
    editor = resp.context["editor_data"]
    assert editor["options"]["instagram"]["alt_text"] == "Three dresses"
    assert "image" in editor["option_caps"]["instagram"]["alt_text"]

    # Clearing a field clears the option.
    _save(client, post_id=post.pk)
    ig.refresh_from_db()
    assert ig.options == {}


def test_a_bad_option_is_refused_and_nothing_is_saved(owner):
    account, client, _ = owner
    resp = _save(client, ig_collaborators="a b c d")
    assert resp.status_code == 302
    assert not ContentPost.objects.filter(account=account).exists()


def test_changing_an_option_after_approval_needs_a_new_review(owner):
    account, client, user = owner
    _save(client, ig_alt_text="Before")
    post = ContentPost.objects.get(account=account)
    post.approval_required = True
    post.approval_status = "approved"
    post.approved_by = user
    post.approved_at = timezone.now()
    post.last_content_hash = post.compute_content_hash()
    post.save()
    assert post.is_approval_valid()

    ig = post.variants.get(platform="instagram")
    ig.options = {"alt_text": "After"}
    ig.save()
    assert not post.is_approval_valid()


def test_a_variant_without_options_keeps_its_old_hash():
    v = ContentPostVariant(platform="instagram", caption="x")
    before = v.compute_content_hash()
    v.options = {}
    assert v.compute_content_hash() == before


# --- Publishing ------------------------------------------------------------------


def _ig_post(account, media_type, options):
    post = ContentPost.objects.create(
        account=account,
        platforms=["instagram"],
        media_type=media_type,
        caption="Hi",
        media_urls=["https://cdn.example.com/a.jpg"],
    )
    variant = ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        caption="Hi",
        media_urls=post.media_urls,
        options=options,
    )
    return post, variant


def _ig_patches(provider):
    ig = MagicMock(instagram_business_account_id="igu")
    return (
        patch("apps.publishing.services._ig_provider_for_post", return_value=provider),
        patch("apps.publishing.services._ig_account", return_value=ig),
    )


def test_instagram_image_sends_alt_text_location_and_collaborators(owner):
    account, _, _ = owner
    post, variant = _ig_post(
        account,
        "image",
        {
            "alt_text": "Dresses",
            "location": {"id": "77", "name": "Lusaka"},
            "collaborators": ["ana"],
            "first_comment": "#sale",  # not a container field
        },
    )
    provider = MagicMock()
    provider.create_image_container.return_value = "c1"
    p1, p2 = _ig_patches(provider)
    with (
        p1,
        p2,
        patch("apps.publishing.services._validate_ig_image_ratio"),
        patch("apps.publishing.services._wait_for_ig_container"),
        patch("apps.publishing.services._ig_publish_container", return_value="m1"),
        patch("apps.publishing.services._ig_permalink", return_value=""),
    ):
        result = _publish_variant_instagram(variant, post)

    assert result["success"]
    provider.create_image_container.assert_called_once_with(
        "igu",
        "https://cdn.example.com/a.jpg",
        "Hi",
        alt_text="Dresses",
        location_id="77",
        collaborators='["ana"]',
    )


def test_instagram_reel_sends_reel_settings(owner):
    account, _, _ = owner
    post, variant = _ig_post(
        account,
        "reel",
        {"share_to_feed": False, "thumb_offset": 3000, "alt_text": "ignored"},
    )
    provider = MagicMock()
    provider.create_video_container.return_value = "c1"
    p1, p2 = _ig_patches(provider)
    with p1, p2:
        _publish_variant_instagram(variant, post)

    provider.create_video_container.assert_called_once_with(
        "igu",
        "https://cdn.example.com/a.jpg",
        "Hi",
        share_to_feed="false",
        thumb_offset=3000,
    )


def test_a_post_without_options_calls_the_provider_as_before(owner):
    account, _, _ = owner
    post, variant = _ig_post(account, "reel", {})
    provider = MagicMock()
    provider.create_video_container.return_value = "c1"
    p1, p2 = _ig_patches(provider)
    with p1, p2:
        _publish_variant_instagram(variant, post)

    provider.create_video_container.assert_called_once_with(
        "igu", "https://cdn.example.com/a.jpg", "Hi"
    )


def test_the_instagram_provider_posts_extra_fields_and_comments():
    from apps.publishing.providers.instagram import InstagramPublishProvider

    provider = InstagramPublishProvider("tok")
    response = MagicMock()
    response.json.return_value = {"id": "x1"}
    with patch(
        "apps.publishing.providers.instagram.requests.post", return_value=response
    ) as post:
        provider.create_image_container("igu", "https://a/b.jpg", "Hi", alt_text="A")
        provider.create_comment("m1", "#sale")

    first, second = post.call_args_list
    assert first.kwargs["data"]["alt_text"] == "A"
    assert second.args[0].endswith("/m1/comments")
    assert second.kwargs["data"]["message"] == "#sale"


# --- First comment ---------------------------------------------------------------


def _published(account, platform="instagram", options=None, media_type="image"):
    post = ContentPost.objects.create(
        account=account,
        platforms=[platform],
        media_type=media_type,
        caption="Hi",
        status=ContentPost.Status.PUBLISHED,
    )
    variant = ContentPostVariant.objects.create(
        post=post,
        platform=platform,
        caption="Hi",
        status=ContentPostVariant.Status.PUBLISHED,
        publication_id="media1",
        published_at=timezone.now(),
        options=options if options is not None else {"first_comment": "#sale"},
    )
    return post, variant


def test_publishing_queues_the_first_comment_once(
    owner, django_capture_on_commit_callbacks
):
    account, _, _ = owner
    post, variant = _published(account)

    with (
        patch("apps.publishing.tasks.post_first_comment_task.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        refresh_parent_post(post)
        refresh_parent_post(post)

    delay.assert_called_once_with(variant.pk)
    variant.refresh_from_db()
    assert variant.metadata[FIRST_COMMENT_KEY]["status"] == "queued"


def test_no_comment_no_queue_and_stories_never_get_one(owner):
    account, _, _ = owner
    _, plain = _published(account, options={})
    _, story = _published(account, media_type="story")
    assert queue_first_comments([plain, story]) == []


def test_posting_the_first_comment(owner):
    account, _, _ = owner
    _, variant = _published(account)
    queue_first_comments([variant])
    provider = MagicMock()
    provider.create_comment.return_value = "cm1"

    with patch("apps.publishing.services._ig_provider_for_post", return_value=provider):
        assert post_first_comment(variant.pk) == "posted"
        # A second run (a duplicate task) does nothing.
        assert post_first_comment(variant.pk) == "posted"

    provider.create_comment.assert_called_once_with("media1", "#sale")
    variant.refresh_from_db()
    assert variant.metadata[FIRST_COMMENT_KEY]["id"] == "cm1"


def test_a_facebook_first_comment_uses_the_page(owner):
    account, _, _ = owner
    _, variant = _published(account, platform="facebook")
    queue_first_comments([variant])
    provider = MagicMock()
    provider.create_comment.return_value = "fbc1"

    with patch(
        "apps.publishing.services._fb_provider_for_post", return_value=(provider, "p")
    ):
        assert post_first_comment(variant.pk) == "posted"
    provider.create_comment.assert_called_once_with("media1", "#sale")


def test_a_permission_error_asks_for_a_reconnect_and_can_be_retried(owner):
    account, client, _ = owner
    post, variant = _published(account)
    queue_first_comments([variant])
    provider = MagicMock()
    provider.create_comment.side_effect = MetaPublishError("no", code=10)

    with patch("apps.publishing.services._ig_provider_for_post", return_value=provider):
        assert post_first_comment(variant.pk) == "failed"
    variant.refresh_from_db()
    state = variant.metadata[FIRST_COMMENT_KEY]
    assert "Reconnect Instagram" in state["error"]

    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()
    assert "Not posted." in html and "Try again" in html

    with patch("apps.publishing.tasks.post_first_comment_task.delay"):
        resp = client.post(
            reverse("publishing:first-comment-retry", args=[post.pk, variant.pk])
        )
    assert resp.status_code == 302
    variant.refresh_from_db()
    assert variant.metadata[FIRST_COMMENT_KEY]["status"] == "queued"


def test_an_unknown_outcome_is_not_retried_blindly(owner):
    account, _, _ = owner
    _, variant = _published(account)
    queue_first_comments([variant])
    provider = MagicMock()
    provider.create_comment.side_effect = TimeoutError("timed out")

    with patch("apps.publishing.services._ig_provider_for_post", return_value=provider):
        assert post_first_comment(variant.pk) == "failed"
    variant.refresh_from_db()
    assert "Check the post" in variant.metadata[FIRST_COMMENT_KEY]["error"]


def test_retry_is_refused_unless_failed(owner):
    account, client, _ = owner
    post, variant = _published(account)
    resp = client.post(
        reverse("publishing:first-comment-retry", args=[post.pk, variant.pk]),
        follow=True,
    )
    assert [str(m) for m in resp.context["messages"]] == [
        "That comment isn't waiting for a retry."
    ]


def test_post_page_lists_channel_options(owner):
    account, client, _ = owner
    post, _ = _published(
        account,
        options={
            "alt_text": "Dresses",
            "collaborators": ["ana"],
            "first_comment": "#sale",
        },
    )
    html = client.get(
        reverse("publishing:post-detail", args=[post.pk])
    ).content.decode()
    assert "Channel options" in html
    assert "Dresses" in html and "@ana" in html
    assert "Posted when the post goes live." in html


# --- Location search -------------------------------------------------------------


def test_location_search_needs_a_facebook_page(owner):
    _, client, _ = owner
    data = client.get(reverse("publishing:location-search"), {"q": "Lusaka"}).json()
    assert "Connect your Facebook Page" in data["error"]


def test_location_search_returns_places(owner):
    account, client, _ = owner
    FacebookPageConnection.objects.create(
        account=account,
        page_id="p1",
        access_token="tok",
        status=FacebookPageConnection.Status.ACTIVE,
    )
    response = MagicMock()
    response.json.return_value = {
        "data": [
            {
                "id": "1",
                "name": "Arcades",
                "location": {"city": "Lusaka", "country": "Zambia"},
            },
            {"id": "2", "name": "No place page"},
        ]
    }
    with patch(
        "apps.publishing.providers.facebook.requests.get", return_value=response
    ):
        data = client.get(reverse("publishing:location-search"), {"q": "Arc"}).json()

    assert data == {
        "results": [{"id": "1", "name": "Arcades", "detail": "Lusaka, Zambia"}]
    }


def test_location_search_explains_when_meta_refuses(owner):
    account, client, _ = owner
    FacebookPageConnection.objects.create(
        account=account,
        page_id="p1",
        access_token="tok",
        status=FacebookPageConnection.Status.ACTIVE,
    )
    response = MagicMock(status_code=400)
    response.json.return_value = {
        "error": {"message": "Feature not available", "code": 10}
    }
    with patch(
        "apps.publishing.providers.facebook.requests.get", return_value=response
    ):
        data = client.get(reverse("publishing:location-search"), {"q": "Arc"}).json()

    assert "isn't available" in data["error"]


def test_facebook_connect_asks_for_comment_permission():
    from apps.publishing.views import _FB_SCOPES

    assert "pages_manage_engagement" in _FB_SCOPES.split(",")

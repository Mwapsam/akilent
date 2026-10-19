"""The studio composer: layout, per-channel captions, the readiness check, autosave and
repeated submits."""

import json
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.http import QueryDict
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.instagram.models import InstagramBusinessAccount
from apps.publishing import readiness
from apps.publishing.composer import (
    ComposerInput,
    StaleVersion,
    destinations,
    save_post,
    version_of,
)
from apps.publishing.models import (
    ContentPost,
    FacebookPageConnection,
    PublishingSettings,
)
from apps.publishing.tasks import publish_post_async

pytestmark = pytest.mark.django_db

IMG = "https://example.com/a.jpg"
VID = "https://example.com/a.mp4"


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Studio Co")
    user = User.objects.create_user("studio", "studio@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, user, client


def _connect_fb(account, **extra):
    return FacebookPageConnection.objects.create(
        account=account,
        page_id="p1",
        page_name="Studio Page",
        access_token="t",
        **extra,
    )


def _connect_ig(account, **extra):
    return InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id="ig1",
        username="studio",
        access_token="t",
        verify_token="v",
        **extra,
    )


def _form(**extra):
    data = {
        "caption": "Hello",
        "platforms": ["facebook"],
        "media_type": "image",
        "media_urls_json": json.dumps([IMG]),
    }
    data.update(extra)
    return data


def _codes(issues):
    return {i["code"] for i in issues}


# --- Layout -----------------------------------------------------------------------------


def test_composer_renders_the_studio_zones(owner):
    _, _, client = owner

    html = client.get(reverse("publishing:post-new")).content.decode()

    for marker in (
        'class="cmp-editor"',
        'class="cmp-preview"',
        'class="cmp-bar"',
        "composer.js",
    ):
        assert marker in html
    # The page scripts load once, after the content (not nested inside it).
    assert html.count("js/cropper.min.js") == 1
    assert html.count("js/composer.js") == 1


def test_editor_data_carries_what_the_page_needs(owner):
    account, _, client = owner
    _connect_fb(account)

    data = client.get(reverse("publishing:post-new")).context["editor_data"]

    assert uuid.UUID(data["client_key"])
    assert data["autosave"] is True
    assert data["constraints"]["caption_max"]["instagram"] == 2200
    assert data["destinations"]["facebook"]["status"] == "connected"
    assert data["destinations"]["instagram"]["status"] == "not_connected"
    assert data["urls"]["readiness"] == reverse("publishing:composer-readiness")


def test_destinations_report_reconnect_needed(owner):
    account, _, _ = owner
    _connect_fb(account, status=FacebookPageConnection.Status.EXPIRED)
    _connect_ig(account, token_expired=True)

    dests = destinations(account)

    assert dests["facebook"]["status"] == "reconnect"
    assert dests["instagram"]["status"] == "reconnect"


def test_a_scheduled_post_opens_with_autosave_off(owner):
    account, _, client = owner
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="text",
        status=ContentPost.Status.SCHEDULED,
        scheduled_at=timezone.now() + timedelta(days=1),
    )

    data = client.get(reverse("publishing:post-new"), {"post_id": post.pk}).context[
        "editor_data"
    ]

    assert data["autosave"] is False
    assert data["version"]


# --- Per-channel captions ---------------------------------------------------------------


def test_a_channel_override_leaves_the_other_channel_on_the_shared_caption(owner):
    account, _, client = owner

    client.post(
        reverse("publishing:post-new"),
        _form(
            platforms=["facebook", "instagram"],
            caption="Shared",
            caption_instagram="Just for Instagram",
            action="draft",
        ),
    )

    post = ContentPost.objects.get(account=account)
    assert post.platform_captions == {"instagram": "Just for Instagram"}
    assert post.variants.get(platform="instagram").caption == "Just for Instagram"
    assert post.variants.get(platform="facebook").caption == "Shared"


def test_clearing_an_override_returns_the_channel_to_the_shared_caption(owner):
    account, _, client = owner
    post = ContentPost.objects.create(
        account=account,
        platforms=["instagram"],
        media_type="image",
        caption="Shared",
        platform_captions={"instagram": "Old custom"},
    )

    client.post(
        reverse("publishing:post-new"),
        _form(
            post_id=post.pk,
            platforms=["instagram"],
            caption="Shared",
            caption_instagram="   ",
            action="draft",
        ),
    )

    post.refresh_from_db()
    assert post.platform_captions == {}
    assert post.variants.get(platform="instagram").caption == "Shared"


# --- Readiness --------------------------------------------------------------------------


def _check(account, **fields):
    inp = ComposerInput(
        platforms=["facebook"], media_type="image", media_urls=[IMG], caption="Hi"
    )
    for k, v in fields.items():
        setattr(inp, k, v)
    return readiness.check(inp, account, wa_ready=True)


def test_nothing_selected_blocks_every_action(owner):
    account, _, _ = owner
    issues = _check(account, platforms=[])
    assert _codes(issues) == {"no_destination"}
    assert set(issues[0]["blocks"]) == set(readiness.GATED_ACTIONS)


def test_content_problems_block_and_point_at_their_field(owner):
    account, _, _ = owner
    _connect_fb(account)
    _connect_ig(account)

    assert "media_missing" in _codes(_check(account, media_urls=[]))
    assert "media_kind" in _codes(_check(account, media_urls=[VID]))
    assert "carousel_count" in _codes(_check(account, media_type="carousel"))
    assert "type_unsupported" in _codes(
        _check(account, platforms=["instagram"], media_type="text", media_urls=[])
    )
    assert "caption_missing" in _codes(
        _check(account, media_type="text", media_urls=[], caption="")
    )
    tags = _check(
        account, platforms=["instagram"], hashtags=[f"t{i}" for i in range(31)]
    )
    assert "too_many_hashtags" in _codes(tags)

    long = _check(account, platforms=["instagram"], caption="x" * 2201)
    issue = next(i for i in long if i["code"] == "caption_too_long")
    assert issue["field"] == "caption"
    assert issue["platform"] == "instagram"

    override = _check(
        account,
        platforms=["instagram"],
        platform_captions={"instagram": "x" * 2300},
    )
    issue = next(i for i in override if i["code"] == "caption_too_long")
    assert issue["field"] == "caption_instagram"


def test_the_call_to_action_and_hashtags_count_toward_the_limit(owner):
    account, _, _ = owner
    _connect_ig(account)
    cta = {"kind": "link", "text": "Shop now", "url": "https://shop.example/x"}

    near = _check(account, platforms=["instagram"], caption="x" * 2190)
    over = _check(account, platforms=["instagram"], caption="x" * 2190, cta=cta)

    assert "caption_too_long" not in _codes(near)
    assert "caption_too_long" in _codes(over)


def test_honest_warnings_do_not_block(owner):
    account, _, _ = owner
    _connect_fb(account)

    issues = _check(account, media_type="carousel", media_urls=[IMG, IMG])

    issue = next(i for i in issues if i["code"] == "fb_first_image_only")
    assert issue["level"] == "warning"
    assert issue["blocks"] == []


def test_a_disconnected_channel_blocks_publishing_now_but_only_warns_a_schedule(owner):
    account, _, _ = owner

    issues = _check(account)

    issue = next(i for i in issues if i["code"] == "facebook_not_connected")
    assert issue["blocks"] == [readiness.PUBLISH_NOW]
    assert readiness.SCHEDULE in issue["warns"]
    assert issue["fix_url"]


def test_schedule_needs_a_future_time(owner):
    account, _, _ = owner
    _connect_fb(account)

    missing = _check(account)
    past = _check(account, scheduled_at=timezone.now() - timedelta(hours=1))
    future = _check(account, scheduled_at=timezone.now() + timedelta(hours=1))

    assert "time_missing" in _codes(missing)
    assert "time_past" in _codes(past)
    assert not readiness.blocking(future, readiness.SCHEDULE)


def test_readiness_endpoint_returns_issues_for_the_form(owner):
    _, _, client = owner

    resp = client.post(
        reverse("publishing:composer-readiness"), _form(media_urls_json="[]")
    )

    assert resp.status_code == 200
    assert "media_missing" in _codes(resp.json()["issues"])


def test_readiness_endpoint_reports_an_unreadable_field(owner):
    _, _, client = owner

    resp = client.post(
        reverse("publishing:composer-readiness"),
        _form(cta_kind="link", cta_url="not a link"),
    )

    issue = resp.json()["issues"][0]
    assert issue["code"] == "invalid"
    assert issue["field"] == "cta"


def test_readiness_endpoint_needs_a_signed_in_user():
    resp = Client().post(reverse("publishing:composer-readiness"), _form())
    assert resp.status_code == 302


def test_publish_now_is_refused_when_something_blocks_it(owner):
    account, _, client = owner  # no Facebook Page connected

    with patch.object(publish_post_async, "delay") as delay:
        client.post(reverse("publishing:post-new"), _form(action="publish_now"))

    delay.assert_not_called()
    assert not ContentPost.objects.filter(account=account).exists()


def test_scheduling_an_image_post_without_an_image_is_refused(owner):
    account, _, client = owner
    _connect_fb(account)

    client.post(
        reverse("publishing:post-new"),
        _form(media_urls_json="[]", scheduled_at="2030-01-01T10:00", action="schedule"),
    )

    assert not ContentPost.objects.filter(account=account).exists()


def test_publishing_an_unconnected_channel_leaves_a_saved_draft_untouched(owner):
    account, _, client = owner  # no Facebook Page connected
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], media_type="image", caption="Kept"
    )

    with patch.object(publish_post_async, "delay") as delay:
        client.post(
            reverse("publishing:post-new"),
            _form(post_id=post.pk, caption="Changed", action="publish_now"),
        )

    delay.assert_not_called()
    post.refresh_from_db()
    assert (post.caption, post.status) == ("Kept", ContentPost.Status.DRAFT)


def test_a_draft_saves_whatever_is_missing(owner):
    account, _, client = owner

    client.post(
        reverse("publishing:post-new"), _form(media_urls_json="[]", action="draft")
    )

    assert ContentPost.objects.get(account=account).status == ContentPost.Status.DRAFT


def test_a_review_workspace_checks_only_content_on_submit(owner):
    account, _, client = owner  # nothing connected
    PublishingSettings.objects.create(account=account, requires_review=True)

    client.post(reverse("publishing:post-new"), _form(action="submit_review"))

    assert ContentPost.objects.get(account=account).approval_status == "submitted"


# --- Autosave ---------------------------------------------------------------------------


def _autosave(client, **extra):
    return client.post(reverse("publishing:composer-autosave"), _form(**extra))


def test_autosave_creates_the_draft_once_for_a_client_key(owner):
    account, _, client = owner
    key = str(uuid.uuid4())

    first = _autosave(client, client_key=key).json()
    second = _autosave(client, client_key=key, caption="Hello again").json()

    assert first["saved"] and second["saved"]
    assert first["post_id"] == second["post_id"]
    post = ContentPost.objects.get(account=account)
    assert post.caption == "Hello again"
    assert post.status == ContentPost.Status.DRAFT
    assert str(post.client_key) == key


def test_autosave_needs_a_key_or_a_post(owner):
    _, _, client = owner
    assert _autosave(client).status_code == 400


def test_autosave_skips_an_empty_or_undirected_draft(owner):
    account, _, client = owner
    key = str(uuid.uuid4())

    empty = _autosave(client, client_key=key, caption="", media_urls_json="[]").json()
    nowhere = _autosave(client, client_key=key, platforms=[]).json()

    assert (empty["reason"], nowhere["reason"]) == ("empty", "no_destination")
    assert not ContentPost.objects.filter(account=account).exists()


def test_autosave_refuses_a_stale_copy_unless_forced(owner):
    account, _, client = owner
    key = str(uuid.uuid4())
    first = _autosave(client, client_key=key).json()
    # Someone else saves the post after this page loaded it.
    _autosave(client, post_id=first["post_id"], caption="Their edit")

    stale = _autosave(
        client, post_id=first["post_id"], version=first["version"], caption="Mine"
    )
    forced = _autosave(
        client,
        post_id=first["post_id"],
        version=first["version"],
        caption="Mine",
        force="1",
    )

    assert stale.status_code == 409
    assert stale.json()["reason"] == "conflict"
    assert forced.json()["saved"]
    assert ContentPost.objects.get(account=account).caption == "Mine"


def test_a_stale_version_is_refused_under_the_row_lock(owner):
    # The view's early check can pass for two tabs at once; the locked one can't.
    account, user, _ = owner
    post = ContentPost.objects.create(
        account=account, platforms=["facebook"], media_type="image", caption="v1"
    )
    seen = version_of(post)
    # Another tab saved (update() skips auto_now, so set the time a save would).
    ContentPost.objects.filter(pk=post.pk).update(
        caption="v2", updated_at=timezone.now() + timedelta(seconds=1)
    )

    with pytest.raises(StaleVersion):
        save_post(
            post,
            ComposerInput(platforms=["facebook"], caption="mine"),
            "draft",
            user,
            QueryDict(),
            expected_version=seen,
        )

    post.refresh_from_db()
    assert post.caption == "v2"


def test_autosave_leaves_a_scheduled_post_alone(owner):
    account, _, client = owner
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="image",
        caption="Planned",
        status=ContentPost.Status.SCHEDULED,
    )

    resp = _autosave(client, post_id=post.pk, caption="Changed")

    assert resp.status_code == 409
    assert resp.json()["reason"] == "not_draft"
    post.refresh_from_db()
    assert (post.caption, post.status) == ("Planned", ContentPost.Status.SCHEDULED)


def test_autosave_cannot_touch_another_workspaces_post(owner):
    _, _, client = owner
    other = Account.objects.create(company_name="Other")
    theirs = ContentPost.objects.create(
        account=other, platforms=["facebook"], media_type="image", caption="Theirs"
    )

    _autosave(client, post_id=theirs.pk, caption="Mine")

    theirs.refresh_from_db()
    assert theirs.caption == "Theirs"


# --- Repeated submits -------------------------------------------------------------------


def test_the_final_submit_reuses_the_autosaved_draft(owner):
    account, _, client = owner
    key = str(uuid.uuid4())
    _autosave(client, client_key=key)

    client.post(reverse("publishing:post-new"), _form(client_key=key, action="draft"))

    assert ContentPost.objects.filter(account=account).count() == 1


def test_a_double_publish_creates_one_post_and_one_publish(owner):
    account, _, client = owner
    _connect_fb(account)
    key = str(uuid.uuid4())

    with patch.object(publish_post_async, "delay") as delay:
        client.post(
            reverse("publishing:post-new"), _form(client_key=key, action="publish_now")
        )
        again = client.post(
            reverse("publishing:post-new"), _form(client_key=key, action="publish_now")
        )

    assert ContentPost.objects.filter(account=account).count() == 1
    delay.assert_called_once()
    post = ContentPost.objects.get(account=account)
    assert again.url == reverse("publishing:post-detail", args=[post.pk])

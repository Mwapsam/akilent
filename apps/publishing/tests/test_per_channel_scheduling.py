"""Per-channel scheduling: each channel publishes at its own time.

Facebook and Instagram variants of one post carry their own time and SCHEDULED status.
The scheduler publishes each when it is due, the post stays scheduled until its last
channel goes out, and times are read in the business's own timezone.
"""

from datetime import UTC, timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, BusinessHours, Membership
from apps.publishing.models import ContentPost, ContentPostVariant, PublishingSettings
from apps.publishing.services import (
    _aggregate_parent_status,
    apply_schedule,
    cancel_post,
    initialize_post_with_variants,
    orchestrate_post_publication,
    reopen_post,
)
from apps.publishing.tasks import publish_due_posts, publish_variant_async

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status


def _ok(publication_id):
    return {
        "success": True,
        "publication_id": publication_id,
        "publication_url": f"https://example.com/{publication_id}",
        "error": "",
        "error_type": "",
        "error_at": None,
        "reconciling": False,
    }


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Stagger Co")
    user = User.objects.create_user("stagger", "stagger@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, user, client


def _scheduled_post(account, fb_at, ig_at):
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook", "instagram"],
        media_type="image",
        caption="Hello",
        media_urls=["https://example.com/a.jpg"],
        status=ContentPost.Status.SCHEDULED,
        scheduled_at=min(fb_at, ig_at),
    )
    initialize_post_with_variants(post, post.platforms)
    apply_schedule(post, {"facebook": fb_at, "instagram": ig_at})
    return post


# --- Scheduling a post marks each channel ---------------------------------------


def test_apply_schedule_gives_each_channel_its_own_time(owner):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(account, now + timedelta(hours=1), now + timedelta(hours=3))

    fb = post.variants.get(platform="facebook")
    ig = post.variants.get(platform="instagram")
    assert (fb.status, ig.status) == (S.SCHEDULED, S.SCHEDULED)
    assert ig.scheduled_at - fb.scheduled_at == timedelta(hours=2)
    post.refresh_from_db()
    assert post.scheduled_at == fb.scheduled_at


def test_a_content_edit_keeps_the_channel_times(owner):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(account, now + timedelta(hours=1), now + timedelta(hours=3))
    ig_time = post.variants.get(platform="instagram").scheduled_at

    post.caption = "Edited"
    post.save()
    initialize_post_with_variants(post, post.platforms)

    assert post.variants.get(platform="instagram").scheduled_at == ig_time


def test_unscheduling_takes_channels_back_to_draft(owner):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(account, now + timedelta(hours=1), now + timedelta(hours=3))

    post.status = ContentPost.Status.DRAFT
    post.save()
    apply_schedule(post)

    assert set(post.variants.values_list("status", flat=True)) == {S.DRAFT}


# --- The scheduler publishes only what is due -----------------------------------


@patch("apps.publishing.services._publish_variant_instagram")
@patch("apps.publishing.services._publish_variant_facebook")
def test_staggered_channels_publish_at_their_own_times(
    mock_fb, mock_ig, owner, django_capture_on_commit_callbacks
):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(
        account, now - timedelta(minutes=1), now + timedelta(hours=2)
    )
    mock_fb.return_value = _ok("fb_1")
    mock_ig.return_value = _ok("ig_1")

    with patch("apps.publishing.tasks.publish_variant_async.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            assert publish_due_posts() == 1
    fb = post.variants.get(platform="facebook")
    delay.assert_called_once_with(fb.pk, scheduled=True)

    assert publish_variant_async(fb.pk, scheduled=True) == "published"
    mock_ig.assert_not_called()

    post.refresh_from_db()
    ig = post.variants.get(platform="instagram")
    assert ig.status == S.SCHEDULED
    # Still waiting for Instagram: not reported as published yet.
    assert post.status == ContentPost.Status.SCHEDULED
    assert post.published_at is None
    assert post.scheduled_at == ig.scheduled_at
    assert post.fb_post_id == "fb_1"

    ig.scheduled_at = now - timedelta(seconds=1)
    ig.save(update_fields=["scheduled_at"])
    assert publish_variant_async(ig.pk, scheduled=True) == "published"
    post.refresh_from_db()
    assert post.status == ContentPost.Status.PUBLISHED
    assert post.published_at is not None


@patch("apps.publishing.services._publish_variant_instagram")
def test_a_channel_not_yet_due_is_not_published(mock_ig, owner):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(account, now + timedelta(hours=1), now + timedelta(hours=2))
    ig = post.variants.get(platform="instagram")

    assert publish_variant_async(ig.pk, scheduled=True) == "not_due"
    mock_ig.assert_not_called()


@patch("apps.publishing.services._publish_variant_facebook")
def test_a_cancelled_channel_dispatched_earlier_is_left_alone(mock_fb, owner):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(
        account, now - timedelta(minutes=1), now + timedelta(hours=2)
    )
    fb = post.variants.get(platform="facebook")
    fb.status = S.CANCELLED
    fb.save(update_fields=["status"])

    assert publish_variant_async(fb.pk, scheduled=True) == "skipped_cancelled"
    mock_fb.assert_not_called()


def test_a_dispatched_channel_is_not_enqueued_again_within_the_grace_window(
    owner, django_capture_on_commit_callbacks
):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(
        account, now - timedelta(minutes=1), now + timedelta(hours=2)
    )

    with patch("apps.publishing.tasks.publish_variant_async.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            assert publish_due_posts() == 1
            assert publish_due_posts() == 0
        # A lost enqueue is retried once the grace window has passed.
        post.variants.filter(platform="facebook").update(
            dispatched_at=now - timedelta(minutes=30)
        )
        with django_capture_on_commit_callbacks(execute=True):
            assert publish_due_posts() == 1
    assert delay.call_count == 2


@patch("apps.publishing.services._publish_variant_instagram")
@patch("apps.publishing.services._publish_variant_facebook")
def test_orchestrator_skips_channels_scheduled_for_later(mock_fb, mock_ig, owner):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(
        account, now - timedelta(minutes=1), now + timedelta(hours=2)
    )
    mock_fb.return_value = _ok("fb_1")
    mock_ig.return_value = _ok("ig_1")

    orchestrate_post_publication(post)
    mock_ig.assert_not_called()

    # "Publish now" (and a workflow step) sends every pending channel.
    orchestrate_post_publication(post, only_due=False)
    mock_ig.assert_called_once()
    post.refresh_from_db()
    assert post.status == ContentPost.Status.PUBLISHED


@patch("apps.publishing.services._publish_variant_facebook")
def test_review_failure_returns_only_that_channel_to_draft(mock_fb, owner):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(
        account, now - timedelta(minutes=1), now - timedelta(minutes=1)
    )
    post.variants.filter(platform="facebook").update(
        status=S.PUBLISHED, publication_id="fb_1"
    )
    PublishingSettings.objects.create(account=account, requires_review=True)
    ig = post.variants.get(platform="instagram")

    assert publish_variant_async(ig.pk, scheduled=True) == "approval_required"

    ig.refresh_from_db()
    post.refresh_from_db()
    assert ig.status == S.DRAFT
    assert "reapprove" in ig.last_error
    # Facebook is live, so the post is not sent back to draft.
    assert post.status == ContentPost.Status.PUBLISHING
    mock_fb.assert_not_called()


# --- Parent status --------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((S.PUBLISHED, S.SCHEDULED), ContentPost.Status.SCHEDULED),
        ((S.PUBLISHED, S.DRAFT), ContentPost.Status.PUBLISHING),
        ((S.PUBLISHED, S.PUBLISHED), ContentPost.Status.PUBLISHED),
        ((S.SCHEDULED, S.SCHEDULED), ContentPost.Status.SCHEDULED),
    ],
)
def test_parent_is_published_only_when_every_channel_is(owner, statuses, expected):
    account, _, _ = owner
    post = ContentPost.objects.create(
        account=account, platforms=["facebook", "instagram"]
    )
    outcomes = [
        {"status": s, "platform": p}
        for s, p in zip(statuses, ("facebook", "instagram"))
    ]
    assert _aggregate_parent_status(post, outcomes) == expected


# --- Cancel and reopen ----------------------------------------------------------


def test_cancel_cancels_every_waiting_channel(owner):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(account, now + timedelta(hours=1), now + timedelta(hours=2))

    cancel_post(post)

    assert set(post.variants.values_list("status", flat=True)) == {S.CANCELLED}
    post.refresh_from_db()
    assert post.status == ContentPost.Status.CANCELLED

    reopen_post(post)
    post.refresh_from_db()
    assert post.status == ContentPost.Status.DRAFT
    assert post.scheduled_at is None
    assert set(post.variants.values_list("status", flat=True)) == {S.DRAFT}


def test_cancel_after_one_channel_went_out_keeps_it_published(owner):
    account, _, _ = owner
    now = timezone.now()
    post = _scheduled_post(account, now - timedelta(hours=1), now + timedelta(hours=2))
    post.variants.filter(platform="facebook").update(
        status=S.PUBLISHED, publication_id="fb_1"
    )

    cancel_post(post)

    post.refresh_from_db()
    assert post.variants.get(platform="instagram").status == S.CANCELLED
    assert post.status == ContentPost.Status.PUBLISHED


def test_cancel_view_cascades_to_channels(owner):
    account, _, client = owner
    now = timezone.now()
    post = _scheduled_post(account, now + timedelta(hours=1), now + timedelta(hours=2))

    client.post(reverse("publishing:post-cancel", args=[post.pk]))

    assert set(post.variants.values_list("status", flat=True)) == {S.CANCELLED}


# --- Composer and timezone ------------------------------------------------------


def test_composer_saves_a_time_per_channel_in_the_business_timezone(owner):
    account, _, client = owner
    BusinessHours.objects.create(account=account, timezone="Africa/Lusaka")  # UTC+2

    client.post(
        reverse("publishing:post-new"),
        {
            "caption": "Launch",
            "platforms": ["facebook", "instagram"],
            "media_type": "image",
            "media_urls_json": '["https://example.com/a.jpg"]',
            "schedule_mode": "per_channel",
            "scheduled_at_facebook": "2030-01-01T10:00",
            "scheduled_at_instagram": "2030-01-01T18:30",
            "action": "schedule",
        },
    )

    post = ContentPost.objects.get(account=account)
    fb = post.variants.get(platform="facebook")
    ig = post.variants.get(platform="instagram")
    assert post.status == ContentPost.Status.SCHEDULED
    assert (fb.status, ig.status) == (S.SCHEDULED, S.SCHEDULED)
    assert fb.scheduled_at.astimezone(UTC).hour == 8
    assert ig.scheduled_at.astimezone(UTC).hour == 16
    assert post.scheduled_at == fb.scheduled_at


def test_composer_same_time_for_every_channel(owner):
    account, _, client = owner

    client.post(
        reverse("publishing:post-new"),
        {
            "caption": "Launch",
            "platforms": ["facebook", "instagram"],
            "media_type": "image",
            "schedule_mode": "same",
            "scheduled_at": "2030-01-01T10:00",
            "action": "schedule",
        },
    )

    post = ContentPost.objects.get(account=account)
    times = set(post.variants.values_list("scheduled_at", flat=True))
    assert times == {post.scheduled_at}


def test_composer_shows_the_business_timezone(owner):
    account, _, client = owner
    BusinessHours.objects.create(account=account, timezone="Africa/Lusaka")

    html = client.get(reverse("publishing:post-new")).content.decode()

    assert "Times are in Africa/Lusaka" in html
    assert "Set your business timezone" not in html


def test_composer_offers_to_set_a_timezone_when_none_is_set(owner):
    _, _, client = owner
    html = client.get(reverse("publishing:post-new")).content.decode()
    assert "Set your business timezone" in html


def test_reschedule_one_channel(owner):
    account, _, client = owner
    now = timezone.now()
    post = _scheduled_post(account, now + timedelta(hours=1), now + timedelta(hours=2))

    client.post(
        reverse("publishing:variant-reschedule", args=[post.pk, "instagram"]),
        {"scheduled_at": "2030-06-01T12:00"},
    )

    ig = post.variants.get(platform="instagram")
    fb = post.variants.get(platform="facebook")
    assert ig.scheduled_at.year == 2030
    assert ig.status == S.SCHEDULED
    assert fb.scheduled_at < ig.scheduled_at


def test_reschedule_refuses_a_published_channel(owner):
    account, _, client = owner
    now = timezone.now()
    post = _scheduled_post(account, now - timedelta(hours=1), now + timedelta(hours=2))
    post.variants.filter(platform="facebook").update(status=S.PUBLISHED)

    client.post(
        reverse("publishing:variant-reschedule", args=[post.pk, "facebook"]),
        {"scheduled_at": "2030-06-01T12:00"},
    )

    assert post.variants.get(platform="facebook").status == S.PUBLISHED


# --- Calendar -------------------------------------------------------------------


def test_calendar_shows_one_entry_per_channel_time(owner):
    account, _, client = owner
    now = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
    _scheduled_post(account, now + timedelta(days=1), now + timedelta(days=1, hours=5))

    week = (now + timedelta(days=1)).isocalendar()
    response = client.get(
        reverse("publishing:calendar"),
        {"week": f"{week[0]}-W{week[1]:02d}", "view": "list"},
    )

    entries = [e for _, _, day in response.context["week_days"] for e in day]
    assert sorted(e["platforms"][0] for e in entries) == ["facebook", "instagram"]


def test_calendar_channel_filter_uses_the_channel(owner):
    account, _, client = owner
    now = timezone.now().replace(hour=9, minute=0, second=0, microsecond=0)
    _scheduled_post(account, now + timedelta(days=1), now + timedelta(days=1, hours=5))

    week = (now + timedelta(days=1)).isocalendar()
    response = client.get(
        reverse("publishing:calendar"),
        {"week": f"{week[0]}-W{week[1]:02d}", "channel": "instagram"},
    )

    entries = [e for _, _, day in response.context["week_days"] for e in day]
    assert [e["platforms"] for e in entries] == [["instagram"]]

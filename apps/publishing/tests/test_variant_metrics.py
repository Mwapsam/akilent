"""Per-channel performance numbers (Phase B).

Each published Facebook/Instagram channel is read on a schedule after publish. Every
metric ends with a number or with a state saying why there is none, and a missing
number is shown as "—", never 0.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.instagram.models import InstagramBusinessAccount
from apps.instagram.oauth import SCOPES
from apps.publishing import metrics
from apps.publishing.metrics import State, metrics_for, next_collection_at
from apps.publishing.metrics_collector import (
    MetricsRateLimited,
    collect_variant_metrics,
)
from apps.publishing.models import (
    ContentPost,
    ContentPostVariant,
    VariantMetricSnapshot,
)
from apps.publishing.providers.errors import parse_insights
from apps.publishing.providers.facebook import (
    FacebookPublishError,
    FacebookPublishProvider,
)
from apps.publishing.providers.instagram import InstagramPublishError
from apps.publishing.services import refresh_parent_post
from apps.publishing.tasks import (
    collect_due_metrics,
    collect_variant_metrics_task,
    fetch_fb_post_insights,
)
from apps.publishing.views import _FB_SCOPES

pytestmark = pytest.mark.django_db

S = ContentPostVariant.Status


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Numbers Co")
    user = User.objects.create_user("numbers", "numbers@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client


def _published(account, platform="instagram", media_type="image", hours_ago=1):
    post = ContentPost.objects.create(
        account=account,
        platforms=[platform],
        media_type=media_type,
        status=ContentPost.Status.PUBLISHED,
    )
    return ContentPostVariant.objects.create(
        post=post,
        platform=platform,
        status=S.PUBLISHED,
        publication_id="17890" if platform == "instagram" else "123_456",
        published_at=timezone.now() - timedelta(hours=hours_ago),
    )


def _ig_provider(fields=None, insights=None):
    """A provider whose reads return the given body, raise the given error, or call it."""
    provider = MagicMock()
    for method, result in (
        (provider.fetch_media_fields, fields),
        (provider.fetch_media_insights, insights),
    ):
        if isinstance(result, Exception) or callable(result):
            method.side_effect = result
        else:
            method.return_value = result or {}
    return provider


def _collect(variant, provider):
    with patch("apps.publishing.metrics_collector._provider", return_value=provider):
        return collect_variant_metrics(variant)


# --- Registry -------------------------------------------------------------------


def test_metrics_follow_the_kind_of_post():
    feed = {m.key for m in metrics_for("instagram", "image")}
    story = {m.key for m in metrics_for("instagram", "story")}
    assert {"reach", "views", "likes", "comments", "saved", "shares"} <= feed
    assert "replies" in story and "likes" not in story and "saved" not in story
    assert {m.key for m in metrics_for("facebook", "image")} == {
        "reach",
        "views",
        "reactions",
        "comments",
        "shares",
    }


def test_no_retired_metric_is_requested():
    names = {m.graph_name for m in metrics.REGISTRY}
    assert not any("impressions" in n for n in names)


def test_every_permission_a_metric_needs_is_requested_at_connect():
    ig_scopes = set(SCOPES.split(","))
    fb_scopes = set(_FB_SCOPES.split(","))
    for m in metrics.REGISTRY:
        assert m.scope in (ig_scopes if m.platform == "instagram" else fb_scopes), m


def test_reads_follow_a_cadence_and_stop():
    published = timezone.now()
    assert next_collection_at(published, "image", published) == published + timedelta(
        hours=1
    )
    assert next_collection_at(
        published, "image", published + timedelta(hours=2)
    ) == published + timedelta(hours=6)
    assert (
        next_collection_at(published, "image", published + timedelta(days=29)) is None
    )
    # Story insights expire after 24 hours.
    assert (
        next_collection_at(
            published, "story", published + timedelta(hours=23, minutes=1)
        )
        is None
    )


# --- Parsing --------------------------------------------------------------------


def test_parse_insights_reads_values_totals_and_breakdowns():
    body = {
        "data": [
            {"name": "reach", "values": [{"value": 120}]},
            {"name": "views", "total_value": {"value": 300}},
            {
                "name": "post_reactions_by_type_total",
                "values": [{"value": {"like": 4, "love": 2}}],
            },
            {"name": "saved", "values": []},
        ]
    }
    assert parse_insights(body) == {
        "reach": 120,
        "views": 300,
        "post_reactions_by_type_total": 6,
    }


# --- Instagram collection -------------------------------------------------------


def test_instagram_numbers_are_stored_and_the_next_read_planned(owner):
    account, _ = owner
    variant = _published(account)
    provider = _ig_provider(
        fields={"like_count": 5, "comments_count": 2},
        insights={
            "reach": 100,
            "views": 150,
            "saved": 3,
            "shares": 1,
            "total_interactions": 11,
        },
    )

    snapshot = _collect(variant, provider)

    assert snapshot.values["reach"] == 100
    assert snapshot.values["likes"] == 5
    assert set(snapshot.states.values()) == {State.AVAILABLE}
    variant.refresh_from_db()
    assert variant.metrics_latest["values"]["views"] == 150
    assert variant.metrics_next_at == variant.published_at + timedelta(hours=6)


def test_a_metric_with_no_data_yet_is_pending_not_zero(owner):
    account, _ = owner
    variant = _published(account)
    provider = _ig_provider(fields={"like_count": 0}, insights={"views": 10})

    snapshot = _collect(variant, provider)

    assert snapshot.values["likes"] == 0  # a real zero stays a zero
    assert snapshot.values["reach"] is None
    assert snapshot.states["reach"] == State.PENDING
    assert snapshot.values["comments"] is None


def test_missing_insights_permission_keeps_likes_and_comments(owner):
    account, _ = owner
    variant = _published(account)
    provider = _ig_provider(
        fields={"like_count": 7, "comments_count": 1},
        insights=InstagramPublishError(
            "(#10) Application does not have permission", code=10
        ),
    )

    snapshot = _collect(variant, provider)

    assert snapshot.values["likes"] == 7
    assert snapshot.states["reach"] == State.NEEDS_PERMISSION
    assert snapshot.states["saved"] == State.NEEDS_PERMISSION


def test_a_story_with_few_viewers_says_so(owner):
    account, _ = owner
    variant = _published(account, media_type="story")
    provider = _ig_provider(
        insights=InstagramPublishError(
            "(#10) Not enough viewers for the media to show insights", code=10
        )
    )

    snapshot = _collect(variant, provider)

    assert snapshot.states["reach"] == State.BELOW_THRESHOLD
    provider.fetch_media_fields.assert_not_called()  # stories have no like/comment fields


def test_an_unsupported_metric_is_found_one_by_one(owner):
    account, _ = owner
    variant = _published(account)

    def insights(media_id, names):
        if len(names) > 1 or names == ["saved"]:
            raise InstagramPublishError("(#100) metric[0] must be one of …", code=100)
        return {names[0]: 9}

    snapshot = _collect(variant, _ig_provider(fields={}, insights=insights))

    assert snapshot.states["saved"] == State.UNSUPPORTED
    assert snapshot.values["saved"] is None
    assert snapshot.values["reach"] == 9


def test_an_expired_token_marks_the_connection(owner):
    account, _ = owner
    ig = InstagramBusinessAccount.objects.create(
        account=account,
        instagram_business_account_id="1784",
        access_token="IGQ-token",
        verify_token="v",
    )
    variant = _published(account)
    expired = InstagramPublishError("Session has expired", code=190)

    snapshot = _collect(variant, _ig_provider(fields=expired, insights=expired))

    assert set(snapshot.states.values()) == {State.NEEDS_PERMISSION}
    ig.refresh_from_db()
    assert ig.token_expired


def test_rate_limit_defers_the_read_without_a_snapshot(owner):
    account, _ = owner
    variant = _published(account)
    limited = InstagramPublishError("Application request limit reached", code=4)

    with pytest.raises(MetricsRateLimited):
        _collect(variant, _ig_provider(fields=limited))
    assert not VariantMetricSnapshot.objects.exists()

    with patch(
        "apps.publishing.metrics_collector._provider",
        return_value=_ig_provider(fields=limited),
    ):
        assert collect_variant_metrics_task(variant.pk) == "rate_limited"
    variant.refresh_from_db()
    assert variant.metrics_next_at > timezone.now() + timedelta(minutes=20)


def test_a_deleted_post_stops_collection(owner):
    account, _ = owner
    variant = _published(account)
    gone = InstagramPublishError("Object does not exist", code=100, subcode=33)

    snapshot = _collect(variant, _ig_provider(fields=gone, insights=gone))

    assert "deleted" in snapshot.error
    variant.refresh_from_db()
    assert variant.metrics_next_at is None


def test_no_connection_asks_to_reconnect(owner):
    account, _ = owner
    variant = _published(account)

    snapshot = _collect(variant, None)

    assert set(snapshot.states.values()) == {State.NEEDS_PERMISSION}


# --- Facebook collection --------------------------------------------------------


def test_facebook_photo_reads_its_post_and_caches_the_id(owner):
    account, _ = owner
    variant = _published(account, platform="facebook")
    variant.publication_id = "998877"  # a photo id, not a post id
    variant.save()
    provider = MagicMock()
    provider.resolve_post_id.return_value = "111_222"
    provider.fetch_fields.return_value = {"comments": {"summary": {"total_count": 4}}}
    provider.fetch_post_insights.return_value = {
        "post_media_view": 80,
        "post_total_media_view_unique": 60,
        "post_reactions_by_type_total": 9,
    }

    snapshot = _collect(variant, provider)

    provider.fetch_post_insights.assert_called_once()
    assert provider.fetch_post_insights.call_args.args[0] == "111_222"
    assert snapshot.values == {
        "reach": 60,
        "views": 80,
        "reactions": 9,
        "comments": 4,
        "shares": 0,  # Facebook leaves shares out of an unshared post
    }
    variant.refresh_from_db()
    assert variant.metadata["fb_post_id"] == "111_222"


def test_resolve_post_id_for_text_and_photo():
    provider = FacebookPublishProvider("page", "token")
    assert provider.resolve_post_id("123_456") == "123_456"

    def fake_get(path, **params):
        if params["fields"] == "page_story_id":
            return {"page_story_id": "123_789"}
        raise AssertionError("unexpected")

    with patch.object(provider, "_get", side_effect=fake_get):
        assert provider.resolve_post_id("789") == "123_789"


def test_resolve_post_id_for_video():
    provider = FacebookPublishProvider("page", "token")

    def fake_get(path, **params):
        if params["fields"] == "page_story_id":
            raise FacebookPublishError(
                "(#100) Tried accessing nonexisting field", code=100
            )
        return {"post_id": "123_555"}

    with patch.object(provider, "_get", side_effect=fake_get):
        assert provider.resolve_post_id("555") == "123_555"


# --- Scheduling of reads --------------------------------------------------------


def test_publishing_plans_the_first_read_an_hour_later(owner):
    account, _ = owner
    variant = _published(account, hours_ago=0)

    refresh_parent_post(variant.post)

    variant.refresh_from_db()
    assert variant.metrics_next_at == variant.published_at + timedelta(hours=1)


def test_the_sweep_queues_due_reads_once(owner, django_capture_on_commit_callbacks):
    account, _ = owner
    due = _published(account)
    due.metrics_next_at = timezone.now() - timedelta(minutes=1)
    due.save()
    later = _published(account)
    later.metrics_next_at = timezone.now() + timedelta(hours=1)
    later.save()

    with patch("apps.publishing.tasks.collect_variant_metrics_task.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            assert collect_due_metrics() == 1
            assert collect_due_metrics() == 0  # leased
    delay.assert_called_once_with(due.pk)


def test_old_queued_facebook_task_still_runs(owner):
    account, _ = owner
    variant = _published(account, platform="facebook")
    with patch(
        "apps.publishing.tasks.collect_variant_metrics_task", return_value="ok"
    ) as task:
        assert fetch_fb_post_insights(variant.post_id) == "ok"
    task.assert_called_once_with(variant.pk)


# --- Views ----------------------------------------------------------------------


def test_performance_card_shows_dash_with_reason_not_zero(owner):
    account, client = owner
    variant = _published(account)
    _collect(
        variant,
        _ig_provider(
            fields={"like_count": 12, "comments_count": 3},
            insights=InstagramPublishError("(#10) permission", code=10),
        ),
    )

    html = client.get(
        reverse("publishing:post-detail", args=[variant.post_id])
    ).content.decode()

    assert "Performance" in html
    assert ">12<" in html
    assert "—" in html
    assert "Reconnect Instagram" in html


def test_performance_card_before_the_first_read(owner):
    account, client = owner
    variant = _published(account, hours_ago=0)
    refresh_parent_post(variant.post)

    html = client.get(
        reverse("publishing:post-detail", args=[variant.post_id])
    ).content.decode()

    assert "First numbers about" in html


def test_refresh_queues_each_published_channel(owner):
    account, client = owner
    variant = _published(account)

    with patch("apps.publishing.tasks.collect_variant_metrics_task.delay") as delay:
        client.post(reverse("publishing:post-refresh-insights", args=[variant.post_id]))
    delay.assert_called_once_with(variant.pk)


def test_refresh_is_throttled(owner):
    account, client = owner
    variant = _published(account)
    variant.metrics_collected_at = timezone.now()
    variant.save()

    with patch("apps.publishing.tasks.collect_variant_metrics_task.delay") as delay:
        client.post(reverse("publishing:post-refresh-insights", args=[variant.post_id]))
    delay.assert_not_called()


def test_refresh_survives_a_broker_outage(owner):
    account, client = owner
    variant = _published(account)

    with patch(
        "apps.publishing.tasks.collect_variant_metrics_task.delay",
        side_effect=ConnectionError,
    ):
        response = client.post(
            reverse("publishing:post-refresh-insights", args=[variant.post_id])
        )
    assert response.status_code == 302


def test_library_shows_reach_per_channel(owner):
    account, client = owner
    variant = _published(account)
    variant.metrics_latest = {
        "values": {"reach": 1204},
        "states": {"reach": "available"},
    }
    variant.save()

    html = client.get(reverse("publishing:library")).content.decode()

    assert "Reach: Instagram 1,204" in html

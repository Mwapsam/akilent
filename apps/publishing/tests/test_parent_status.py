"""Parent post status and error follow its variants from every transition."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import (
    orchestrate_post_publication,
    reconcile_variant_confirmed_published,
)
from apps.publishing.tasks import poll_ig_variant_containers

pytestmark = pytest.mark.django_db

FB_OK = {
    "success": True,
    "publication_id": "fb_1",
    "publication_url": "https://facebook.com/fb_1",
    "error": "",
    "error_type": "",
    "error_at": None,
    "reconciling": False,
}


@pytest.fixture
def account():
    return Account.objects.create(company_name="Parent Co")


def _post(account, platforms):
    return ContentPost.objects.create(
        account=account,
        platforms=platforms,
        media_type="image",
        status=ContentPost.Status.PUBLISHING,
    )


def test_processing_instagram_video_completing_marks_parent_published(account):
    post = _post(account, ["instagram"])
    ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=ContentPostVariant.Status.PROCESSING,
        processing_started_at=timezone.now(),
        metadata={"ig_container_id": "c1"},
    )
    provider = MagicMock()
    provider.get_permalink.return_value = ""
    provider.poll_container_status.return_value = "FINISHED"
    provider.publish_container.return_value = "17912345"

    with patch("apps.publishing.services._ig_provider_for_post", return_value=provider):
        poll_ig_variant_containers()

    post.refresh_from_db()
    assert post.status == ContentPost.Status.PUBLISHED
    assert post.published_at is not None
    assert post.error == ""


def test_partial_success_stays_nonterminal_and_names_the_failed_channel(account):
    post = _post(account, ["facebook", "instagram"])
    ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        status=ContentPostVariant.Status.PUBLISHED,
        publication_id="fb_1",
    )
    ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=ContentPostVariant.Status.FAILED,
        last_error="Rate limited",
    )
    post.refresh_from_db()

    from apps.publishing.services import refresh_parent_post

    refresh_parent_post(post)

    post.refresh_from_db()
    assert post.status == ContentPost.Status.PUBLISHING
    assert post.published_at is None
    assert post.error == "Instagram: Rate limited"


def test_processing_is_not_reported_as_an_error(account):
    post = _post(account, ["instagram"])
    ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=ContentPostVariant.Status.PROCESSING,
        last_error="Video publishing in progress",
        processing_started_at=timezone.now(),
    )
    from apps.publishing.services import refresh_parent_post

    refresh_parent_post(post)

    post.refresh_from_db()
    assert post.error == ""


def test_reconciliation_confirmation_marks_parent_published(account):
    user = User.objects.create_user("op", "op@example.com", "pass")
    post = _post(account, ["facebook"])
    variant = ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        status=ContentPostVariant.Status.RECONCILING,
        last_error="Final publication timed out",
    )

    reconcile_variant_confirmed_published(
        variant=variant,
        publication_id="fb_9",
        publication_url="https://facebook.com/fb_9",
        evidence_notes="seen on page",
        performed_by=user,
    )

    post.refresh_from_db()
    assert post.status == ContentPost.Status.PUBLISHED
    assert post.published_at is not None


def test_workflow_publication_persists_parent_status(account):
    post = _post(account, ["facebook"])
    ContentPostVariant.objects.create(post=post, platform="facebook", caption="x")
    post.approval_required = False
    post.save()

    with patch(
        "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
    ):
        orchestrate_post_publication(post)

    post.refresh_from_db()
    assert post.status == ContentPost.Status.PUBLISHED
    assert post.published_at is not None


def test_legacy_facebook_failure_is_kept_while_instagram_video_processes(account):
    from apps.publishing.providers.facebook import FacebookPublishError
    from apps.publishing.services import publish_post

    post = _post(account, ["facebook", "instagram"])

    def ig(p):
        p.ig_container_id = "container_1"

    with (
        patch("apps.publishing.services._publish_instagram", side_effect=ig),
        patch(
            "apps.publishing.services._publish_facebook",
            side_effect=FacebookPublishError("FB down"),
        ),
    ):
        publish_post(post)

    post.refresh_from_db()
    assert post.status == ContentPost.Status.PUBLISHING
    assert "Facebook: FB down" in post.error


def test_instagram_publication_url_is_the_real_permalink(account):
    from apps.publishing.services import _publish_variant_instagram

    post = _post(account, ["instagram"])
    variant = ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        caption="x",
        media_urls=["https://cdn.example.com/a.jpg"],
    )
    provider = MagicMock()
    provider.get_permalink.return_value = ""
    provider.create_image_container.return_value = "c1"
    provider.poll_container_status.return_value = "FINISHED"
    provider.publish_container.return_value = "17912345"
    provider.get_permalink.return_value = "https://www.instagram.com/p/ABC123/"

    with (
        patch("apps.publishing.services._ig_provider_for_post", return_value=provider),
        patch(
            "apps.publishing.services._ig_account",
            return_value=MagicMock(instagram_business_account_id="ig_user"),
        ),
        patch("apps.publishing.services._validate_ig_image_ratio"),
        patch("apps.publishing.services._wait_for_ig_container"),
    ):
        result = _publish_variant_instagram(variant, post)

    assert result["publication_url"] == "https://www.instagram.com/p/ABC123/"


def test_permalink_lookup_failure_keeps_the_publication_successful(account):
    from apps.publishing.services import _ig_permalink

    provider = MagicMock()

    provider.get_permalink.return_value = ""
    provider.get_permalink.side_effect = RuntimeError("graph down")

    assert _ig_permalink(provider, "17912345") == ""


def test_legacy_in_flight_container_migration_makes_it_pollable_and_spares_others(
    account,
):
    import importlib

    from django.apps import apps as global_apps

    migration = importlib.import_module(
        "apps.publishing.migrations.0010_reclaim_legacy_ig_containers"
    )
    post = _post(account, ["instagram"])
    in_flight = ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status="publishing",
        metadata={"ig_container_id": "c9"},
    )
    no_container = ContentPostVariant.objects.create(
        post=post, platform="facebook", status="publishing"
    )
    published = ContentPostVariant.objects.create(
        post=_post(account, ["instagram"]),
        platform="instagram",
        status="published",
        publication_id="ig_1",
        metadata={"ig_container_id": "c8"},
    )

    migration.reclaim_in_flight_containers(global_apps, None)

    in_flight.refresh_from_db()
    no_container.refresh_from_db()
    published.refresh_from_db()
    assert in_flight.status == "processing"
    assert in_flight.processing_started_at is not None
    assert no_container.status == "publishing"
    assert published.status == "published"


def test_cancelled_channel_does_not_block_parent_publication(account):
    from apps.publishing.services import refresh_parent_post

    post = _post(account, ["facebook", "instagram"])
    ContentPostVariant.objects.create(
        post=post,
        platform="facebook",
        status=ContentPostVariant.Status.PUBLISHED,
        publication_id="fb_1",
    )
    ContentPostVariant.objects.create(
        post=post, platform="instagram", status=ContentPostVariant.Status.CANCELLED
    )

    refresh_parent_post(post)

    post.refresh_from_db()
    assert post.status == ContentPost.Status.PUBLISHED
    assert post.published_at is not None


def test_invalid_approval_returns_post_to_draft_without_publishing(account):
    from apps.publishing.tasks import publish_post_async

    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="image",
        status=ContentPost.Status.SCHEDULED,
        approval_required=True,
        approval_status="approved",
        approved_at=timezone.now(),
        last_content_hash="stale",
    )
    ContentPostVariant.objects.create(post=post, platform="facebook", caption="edited")

    with patch(
        "apps.publishing.services._publish_variant_facebook", return_value=FB_OK
    ) as fb:
        outcome = publish_post_async(post.pk)

    post.refresh_from_db()
    assert outcome == "approval_required"
    assert post.status == ContentPost.Status.DRAFT
    assert "reapprove" in post.error
    fb.assert_not_called()

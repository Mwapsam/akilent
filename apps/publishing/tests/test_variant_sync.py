"""Editing a post reconciles its channel variants without touching publication history."""

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import initialize_post_with_variants

pytestmark = pytest.mark.django_db
Status = ContentPostVariant.Status


@pytest.fixture
def post():
    account = Account.objects.create(company_name="Sync Co")
    return ContentPost.objects.create(
        account=account,
        platforms=["facebook", "instagram"],
        caption="A",
        media_urls=["https://cdn.example.com/a.jpg"],
        media_type="image",
    )


def test_edit_updates_draft_variant_content(post):
    initialize_post_with_variants(post, ["facebook", "instagram"])
    post.caption = "B"
    post.media_urls = ["https://cdn.example.com/b.jpg"]
    post.save()

    initialize_post_with_variants(post, ["facebook", "instagram"])

    for v in ContentPostVariant.objects.filter(post=post):
        assert v.caption == "B"
        assert v.media_urls == ["https://cdn.example.com/b.jpg"]


def test_platform_override_is_used_for_that_channel(post):
    post.platform_captions = {"facebook": "FB only"}
    post.save()

    initialize_post_with_variants(post, ["facebook", "instagram"])

    assert (
        ContentPostVariant.objects.get(post=post, platform="facebook").caption
        == "FB only"
    )
    assert (
        ContentPostVariant.objects.get(post=post, platform="instagram").caption == "A"
    )


def test_deselected_unpublished_channel_is_cancelled(post):
    initialize_post_with_variants(post, ["facebook", "instagram"])

    initialize_post_with_variants(post, ["facebook"])

    assert (
        ContentPostVariant.objects.get(post=post, platform="instagram").status
        == Status.CANCELLED
    )


def test_deselected_published_channel_keeps_its_history(post):
    ig = ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=Status.PUBLISHED,
        publication_id="ig_1",
        publication_url="https://www.instagram.com/p/X/",
        published_at=timezone.now(),
    )

    initialize_post_with_variants(post, ["facebook"])

    ig.refresh_from_db()
    assert ig.status == Status.PUBLISHED
    assert ig.publication_id == "ig_1"


def test_in_flight_variant_is_not_overwritten_by_an_edit(post):
    ig = ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=Status.PROCESSING,
        caption="original",
    )
    post.caption = "edited"
    post.save()

    initialize_post_with_variants(post, ["facebook", "instagram"])

    ig.refresh_from_db()
    assert ig.caption == "original"
    assert ig.status == Status.PROCESSING


def test_reselecting_a_cancelled_channel_reopens_it_as_draft(post):
    initialize_post_with_variants(post, ["facebook", "instagram"])
    initialize_post_with_variants(post, ["facebook"])
    post.caption = "C"
    post.save()

    initialize_post_with_variants(post, ["facebook", "instagram"])

    ig = ContentPostVariant.objects.get(post=post, platform="instagram")
    assert ig.status == Status.DRAFT
    assert ig.caption == "C"

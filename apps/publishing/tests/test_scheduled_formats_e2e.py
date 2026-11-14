"""Scheduled posts of every Instagram format, end to end through the real scheduler tasks
(publish_due_posts, then the container poller), with only Meta's answers mocked."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.providers.instagram import CONTAINER_STATUS_FINISHED

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    from apps.instagram.models import InstagramBusinessAccount

    user = User.objects.create_user("u", "u@example.com", "p")
    account = Account.objects.create(company_name="A")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    InstagramBusinessAccount.objects.create(
        account=account, instagram_business_account_id="1", access_token="t", is_active=True
    )
    return account


MEDIA = {
    "carousel": ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"],
    "reel": ["https://cdn.example.com/v.mp4"],
    "video": ["https://cdn.example.com/v.mp4"],
}


@pytest.mark.parametrize("mt", ["carousel", "reel", "video", "image"])
def test_scheduled_format_publishes(mt, account, django_capture_on_commit_callbacks):
    from apps.publishing import tasks
    from apps.publishing.services import apply_schedule, initialize_post_with_variants

    post = ContentPost.objects.create(
        account=account,
        platforms=["instagram"],
        media_type=mt,
        media_urls=MEDIA.get(mt, ["https://cdn.example.com/i.jpg"]),
        caption="hi",
        status=ContentPost.Status.SCHEDULED,
        scheduled_at=timezone.now() - timedelta(minutes=1),
    )
    initialize_post_with_variants(post, ["instagram"])
    apply_schedule(post)

    provider = MagicMock()
    for m in ("create_image_container", "create_video_container", "create_carousel_item_container",
              "create_carousel_container"):
        getattr(provider, m).return_value = f"c-{m}"
    provider.poll_container_status.return_value = CONTAINER_STATUS_FINISHED
    provider.publish_container.return_value = "media-1"
    provider.get_permalink.return_value = "https://instagram.com/p/x"

    stages = []
    def snap(label):
        post.refresh_from_db()
        v = post.variants.get()
        stages.append(f"{label}: post={post.status} var={v.status} err={v.last_error!r} md={v.metadata}")

    snap("scheduled")
    with patch("apps.publishing.services._ig_provider_for_post", return_value=provider), \
         patch("apps.publishing.services._validate_ig_image_ratio"), \
         patch("apps.publishing.tasks.publish_variant_async.delay",
               side_effect=lambda *a, **k: tasks.publish_variant_async.apply(args=a, kwargs=k)), \
         patch("apps.publishing.tasks.publish_post_async.delay",
               side_effect=lambda *a, **k: tasks.publish_post_async.apply(args=a, kwargs=k)):
        with django_capture_on_commit_callbacks(execute=True):
            n = tasks.publish_due_posts()
        snap(f"after publish_due_posts (dispatched {n})")
        tasks.poll_ig_variant_containers()
        snap("after poll")
    post.refresh_from_db()
    assert post.status == ContentPost.Status.PUBLISHED, "\n".join(stages)

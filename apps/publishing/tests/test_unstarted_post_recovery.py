from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.tasks import (
    publish_due_posts,
    publish_post_async,
    recover_unstarted_publishing_posts,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    return Account.objects.create(company_name="Stuck Co")


def _publishing_post(account, variant_status, minutes_ago=30):
    post = ContentPost.objects.create(
        account=account,
        platforms=["facebook"],
        media_type="text",
        status=ContentPost.Status.PUBLISHING,
    )
    ContentPostVariant.objects.create(
        post=post, platform="facebook", status=variant_status
    )
    ContentPost.objects.filter(pk=post.pk).update(
        updated_at=timezone.now() - timedelta(minutes=minutes_ago)
    )
    return post


def test_publish_now_enqueue_failure_leaves_a_draft_not_a_stuck_post(account):
    user = User.objects.create_user("maker", "maker@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)

    with patch.object(
        publish_post_async, "delay", side_effect=ConnectionError("broker down")
    ):
        client.post(
            reverse("publishing:post-new"),
            {
                "caption": "hello",
                "media_type": "text",
                "platforms": ["facebook"],
                "action": "publish_now",
            },
        )

    post = ContentPost.objects.get(account=account)
    assert post.status == ContentPost.Status.DRAFT


def test_unstarted_publishing_post_is_re_enqueued(account):
    post = _publishing_post(account, ContentPostVariant.Status.DRAFT)

    with patch.object(publish_post_async, "delay") as delay:
        assert recover_unstarted_publishing_posts() == 1

    delay.assert_called_once_with(post.pk)


def test_recently_started_post_is_left_alone(account):
    _publishing_post(account, ContentPostVariant.Status.DRAFT, minutes_ago=1)

    with patch.object(publish_post_async, "delay") as delay:
        assert recover_unstarted_publishing_posts() == 0

    delay.assert_not_called()


def test_post_with_a_claimed_variant_is_left_to_the_variant_reconcilers(account):
    _publishing_post(account, ContentPostVariant.Status.PUBLISHING)

    with patch.object(publish_post_async, "delay") as delay:
        assert recover_unstarted_publishing_posts() == 0

    delay.assert_not_called()


def test_recovery_retries_each_post_at_most_once_per_grace_window(account):
    _publishing_post(account, ContentPostVariant.Status.DRAFT)

    with patch.object(publish_post_async, "delay") as delay:
        recover_unstarted_publishing_posts()
        recover_unstarted_publishing_posts()

    assert delay.call_count == 1


def test_scheduler_broker_failure_does_not_roll_back_the_batch(
    account, django_capture_on_commit_callbacks
):
    due = timezone.now() - timedelta(minutes=1)
    posts = [
        ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            media_type="text",
            status=ContentPost.Status.SCHEDULED,
            scheduled_at=due,
        )
        for _ in range(2)
    ]

    with (
        patch.object(
            publish_post_async, "delay", side_effect=ConnectionError("broker down")
        ),
        django_capture_on_commit_callbacks(execute=True),
    ):
        assert publish_due_posts() == 2

    for post in posts:
        post.refresh_from_db()
        assert post.status == ContentPost.Status.PUBLISHING

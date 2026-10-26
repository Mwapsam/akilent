"""The Facebook channel switch covers both URL prefixes; shared comment rules and the
Comments view keep Instagram working as before."""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.test import override_settings

from apps.billing.models import AccountFeatureOverride
from apps.instagram.models import Comment, CommentThread, ModerationLog, ModerationRule
from apps.instagram.models.moderation import RuleChannels
from apps.instagram.providers.base import SendResult
from apps.instagram.tests.helpers import (
    make_account,
    make_instagram_account,
    make_instagram_contact,
)

pytestmark = pytest.mark.django_db


def test_switching_facebook_off_closes_its_pages_but_keeps_callbacks(client):
    from apps.core.channels import invalidate_channel_cache

    account, user = make_account()
    client.force_login(user)
    with override_settings(FACEBOOK_ENABLED=False):
        invalidate_channel_cache()
        assert client.get("/publishing/fb/connect/").status_code == 404
        # Not set up on this server: a webhook POST is acknowledged and dropped.
        assert (
            client.post(
                "/facebook/webhook/", data="{}", content_type="application/json"
            ).status_code
            == 200
        )
        # Meta's compliance callbacks stay reachable (400: no valid signed_request).
        assert client.post("/facebook/data-deletion/").status_code == 400
    invalidate_channel_cache()


def test_existing_rules_stay_instagram_only_after_the_migration():
    migration = importlib.import_module(
        "apps.instagram.migrations.0020_shared_rule_channels"
    )
    account, _user = make_account()
    rule = ModerationRule.objects.create(
        account=account,
        name="Old",
        match_type=ModerationRule.MatchType.KEYWORD,
        keywords="x",
        moderation_action="hide",
        channels=RuleChannels.BOTH,
    )
    migration.existing_rules_stay_instagram(django_apps, None)
    rule.refresh_from_db()
    assert rule.channels == RuleChannels.INSTAGRAM


def _ig_comment(account):
    from django.utils import timezone

    iba = make_instagram_account(account)
    ig_contact, _contact = make_instagram_contact(account, iba)
    thread = CommentThread.objects.create(
        instagram_account=iba,
        post_id="media_1",
        instagram_contact=ig_contact,
        comment_id="c_1",
        body="Love it",
        received_at=timezone.now(),
    )
    comment = Comment.objects.create(
        thread=thread,
        comment_id="c_1",
        instagram_contact=ig_contact,
        body="Love it",
        timestamp=timezone.now(),
    )
    return iba, comment


def test_comments_view_works_for_instagram_too():
    from apps.conversations import comments

    account, user = make_account()
    AccountFeatureOverride.objects.create(account=account, key="instagram", grant=True)
    _iba, comment = _ig_comment(account)

    P = "apps.instagram.providers.meta.MetaInstagramProvider"
    with (
        patch(f"{P}.comment_on_media", return_value=SendResult(True, "ig_c9")) as post,
        patch(f"{P}.reply_to_comment", return_value=SendResult(True, "ig_r1")),
        patch(f"{P}.hide_comment", return_value=(True, "")) as hide,
    ):
        comments.post_comment(
            account, "instagram", "media_1", "New colours!", user, "a"
        )
        comments.reply(account, "instagram", comment.pk, "Thank you!", user, "b")
        comments.moderate(account, "instagram", comment.pk, "hide", user)

    post.assert_called_once_with("media_1", "New colours!")
    hide.assert_called_once_with("c_1", True)
    assert Comment.objects.filter(direction="outbound", body="Thank you!").exists()
    log = ModerationLog.objects.get(comment=comment)
    assert log.acted_by == user and log.facebook_comment is None
    posts = comments.posts_with_comments(account)
    assert posts and posts[0].business_comments[0].body == "New colours!"

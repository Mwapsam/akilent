"""Slack alerts: repeated failures of the same kind post once per cooldown."""

from unittest.mock import patch

import pytest
from django.core.cache import cache

from apps.billing.slack import post_message


@pytest.fixture(autouse=True)
def _webhook(settings):
    settings.SLACK_WEBHOOK_URL = "https://hooks.slack.test/x"
    cache.clear()
    yield
    cache.clear()


def test_without_a_key_every_message_posts():
    with patch("apps.billing.slack.requests.post") as post:
        assert post_message("a") and post_message("a")
    assert post.call_count == 2


def test_a_keyed_alert_posts_once_per_cooldown():
    with patch("apps.billing.slack.requests.post") as post:
        assert post_message("boom", dedupe_key="k") is True
        assert post_message("boom again", dedupe_key="k") is False
        assert post_message("other", dedupe_key="k2") is True
    assert post.call_count == 2


def test_no_webhook_posts_nothing(settings):
    settings.SLACK_WEBHOOK_URL = ""
    with patch("apps.billing.slack.requests.post") as post:
        assert post_message("x", dedupe_key="k") is False
    post.assert_not_called()
    # Not configured must not burn the cooldown for when it is.
    assert cache.get("slack-alert:k") is None

"""UI quality programme, Phase 2: Content Studio says what happened, truthfully.

The composer flows (phone layout, unsaved-work guard, confirm and crop dialogs) run in the
browser: tools/qa/publishing_qa.mjs. These hold the server-rendered parts.
"""

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.accounts.models import Account, Membership
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.templatetags.publishing_filters import variant_error_message

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner():
    account = Account.objects.create(company_name="Studio Co")
    user = User.objects.create_user("studio", "studio@example.com", "pass")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client = Client()
    client.force_login(user)
    return account, client


def _variant(error_type, error="Upload rejected", platform="instagram"):
    return SimpleNamespace(
        last_error=error, last_error_type=error_type, platform=platform
    )


class TestChannelErrorWording:
    def test_a_timeout_says_it_may_already_be_live(self):
        msg = variant_error_message(_variant("timeout"))
        assert "may already be live" in msg and "Check it on Instagram" in msg

    def test_an_outage_never_promises_an_automatic_retry(self):
        """A provider outage is saved as FAILED; nothing retries it on its own."""
        msg = variant_error_message(_variant("provider_outage", platform="facebook"))
        assert "will retry" not in msg and "Facebook had a problem" in msg

    def test_any_channel_is_named_mid_sentence(self):
        msg = variant_error_message(_variant("permission", platform="threads"))
        assert "Reconnect Threads in Channels" in msg and "The channel" not in msg
        msg = variant_error_message(_variant("permission", platform=""))
        assert "Reconnect the channel in Channels" in msg
        msg = variant_error_message(_variant("timeout", platform=""))
        assert "The channel didn't answer" in msg and "Check it on the channel" in msg

    def test_no_double_full_stop(self):
        assert ".." not in variant_error_message(
            _variant("permission", "Token expired.")
        )

    def test_the_providers_own_punctuation_is_kept_once(self):
        msg = variant_error_message(_variant("provider_outage", "Media upload failed!"))
        assert msg.startswith("Media upload failed! Instagram had a problem")
        msg = variant_error_message(_variant("permission", "Invalid token (code 190)"))
        assert msg.startswith("Invalid token (code 190). Reconnect Instagram")


def test_a_failed_channel_shows_its_reason_on_the_post(owner):
    account, client = owner
    post = ContentPost.objects.create(
        account=account, platforms=["instagram", "facebook"], caption="Hello"
    )
    ContentPostVariant.objects.create(
        post=post,
        platform="instagram",
        status=ContentPostVariant.Status.FAILED,
        last_error="Image too small",
        last_error_type="validation",
    )
    body = client.get(f"/publishing/posts/{post.pk}/").content.decode()
    assert 'id="pd-channels"' in body and ">Channels</h2>" in body
    assert "Image too small. Change the post to fix this, then try again." in body


def test_the_library_tells_no_posts_from_filtered_out(owner):
    _, client = owner
    empty = client.get("/publishing/library/").content.decode()
    assert "No posts yet" in empty and "No posts match" not in empty
    filtered = client.get("/publishing/library/?q=nothing").content.decode()
    assert "No posts match these filters" in filtered


def test_an_error_of_only_whitespace_says_nothing():
    assert variant_error_message(_variant("permission", "\n  ")) == ""


def test_the_week_summary_counts_posts_not_channel_times(owner):
    """A post with a different time per channel is one post (failed once if both failed)."""
    import datetime

    from django.utils import timezone

    account, client = owner
    monday = timezone.localdate() - datetime.timedelta(
        days=timezone.localdate().weekday()
    )
    at = timezone.make_aware(datetime.datetime.combine(monday, datetime.time(9)))
    post = ContentPost.objects.create(
        account=account,
        platforms=["instagram", "facebook"],
        caption="Two times",
        status=ContentPost.Status.FAILED,
        scheduled_at=at,
    )
    for platform, hours in (("instagram", 0), ("facebook", 3)):
        ContentPostVariant.objects.create(
            post=post,
            platform=platform,
            status=ContentPostVariant.Status.FAILED,
            scheduled_at=at + datetime.timedelta(hours=hours),
        )
    response = client.get("/publishing/calendar/")
    entries = [e for _, _, day in response.context["week_days"] for e in day]
    assert len(entries) == 2
    assert response.context["week_summary"]["posts"] == 1
    assert response.context["week_summary"]["failed"] == 1

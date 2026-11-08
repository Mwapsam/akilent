"""Managing the Facebook Page like the other channels: the Channels card, the Facebook
page, connecting without Content Studio, and comment rules without Instagram."""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.billing.models import AccountFeatureOverride
from apps.facebook.tests.helpers import make_page
from apps.instagram.api import ModerationRule, RuleChannels

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_channel_cache():
    from apps.core.channels import invalidate_channel_cache

    invalidate_channel_cache()
    yield
    invalidate_channel_cache()


def test_channels_page_lists_facebook(client):
    account, user, conn = make_page()
    conn.webhook_subscribed_at = None
    conn.save(update_fields=["webhook_subscribed_at"])
    client.force_login(user)

    html = client.get("/channels/").content.decode()

    assert "Manage Facebook" in html
    assert "Reconnect needed" in html


def test_facebook_page_shows_the_page_and_its_rules(client):
    account, user, conn = make_page()
    ModerationRule.objects.create(
        account=account,
        name="Spam",
        match_type=ModerationRule.MatchType.KEYWORD,
        keywords="spam",
        moderation_action="hide",
        channels=RuleChannels.BOTH,
    )
    ModerationRule.objects.create(
        account=account,
        name="IG only",
        match_type=ModerationRule.MatchType.KEYWORD,
        keywords="x",
        moderation_action="hide",
        channels=RuleChannels.INSTAGRAM,
    )
    client.force_login(user)

    html = client.get("/facebook/").content.decode()

    assert "Test Page" in html and "Connected" in html
    assert "1 rule covers Facebook comments." in html
    assert "/instagram/rules/" in html


@override_settings(WHATSAPP_APP_ID="1234")
def test_facebook_page_without_a_page_offers_to_connect(client):
    account, user, conn = make_page()
    conn.delete()
    client.force_login(user)

    html = client.get("/facebook/").content.decode()

    assert "No Facebook Page connected" in html
    assert "/publishing/fb/connect/?next=facebook-page" in html


@override_settings(WHATSAPP_APP_ID="")
def test_connect_needs_only_the_facebook_inbox_and_returns_to_the_facebook_page(
    client,
):
    from apps.billing import api as billing_api

    account, user, conn = make_page()
    AccountFeatureOverride.objects.create(
        account=account, key="content_studio", grant=False
    )
    assert not billing_api.usable(account, "content_studio")
    client.force_login(user)

    response = client.get("/publishing/fb/connect/?next=facebook-page")

    # Not sent to the Content Studio locked page; the app id is missing here, so the
    # flow ends at once, back where it began.
    assert response.status_code == 302
    assert response["Location"] == "/facebook/"


def test_connect_ignores_an_unknown_next(client):
    account, user, conn = make_page()
    client.force_login(user)
    session = client.session
    session["fb_connect_return"] = "facebook-page"
    session.save()

    with override_settings(WHATSAPP_APP_ID=""):
        response = client.get("/publishing/fb/connect/?next=https://evil.example/")

    assert response["Location"] == "/settings/integrations/"


def test_disconnect_returns_to_the_facebook_page(client, monkeypatch):
    account, user, conn = make_page()
    monkeypatch.setattr(
        "apps.facebook.providers.meta.MetaFacebookProvider.unsubscribe_page",
        lambda self: None,
    )
    client.force_login(user)

    response = client.post("/publishing/fb/disconnect/", {"next": "facebook-page"})

    assert response["Location"] == "/facebook/"


def test_comment_rules_open_with_instagram_switched_off(client):
    account, user, conn = make_page()
    client.force_login(user)

    with override_settings(INSTAGRAM_ENABLED=False):
        from apps.core.channels import invalidate_channel_cache

        invalidate_channel_cache()
        assert client.get("/instagram/rules/").status_code == 200
        # The rest of Instagram stays closed.
        assert client.get("/instagram/accounts/").status_code == 404
        with override_settings(FACEBOOK_ENABLED=False):
            invalidate_channel_cache()
            assert client.get("/instagram/rules/").status_code == 404


def test_without_the_facebook_inbox_there_is_no_rules_card(client):
    account, user, conn = make_page()
    AccountFeatureOverride.objects.filter(account=account, key="facebook").delete()
    client.force_login(user)

    html = client.get("/facebook/").content.decode()
    page = html[html.index('<main id="main"') : html.index("</main>")]

    assert "Comment rules" not in page


def test_diagnose_explains_an_instagram_only_rule():
    from io import StringIO

    from django.core.management import call_command

    from apps.facebook.models.webhook import FacebookWebhookEventLog
    from apps.facebook.tests.helpers import comment_payload
    from apps.instagram.api import CommentTrigger

    account, user, conn = make_page()
    CommentTrigger.objects.create(
        account=account,
        name="Price",
        match_type=CommentTrigger.MatchType.KEYWORD,
        keywords="price",
        reply_template="Hi!",
        channels=RuleChannels.INSTAGRAM,
    )
    FacebookWebhookEventLog.objects.create(
        account=account,
        page_id=conn.page_id,
        event_id="c1:add",
        event_type=FacebookWebhookEventLog.EventType.COMMENT,
        raw_payload=comment_payload(conn.page_id, "psid1", "What price?"),
    )
    out = StringIO()

    call_command("facebook_diagnose", stdout=out)

    text = out.getvalue()
    assert "0 cover Facebook, 1 are Instagram-only" in text
    assert "no active reply rule covering Facebook matches" in text


def test_rules_page_offers_to_apply_instagram_only_rules_to_facebook(client):
    from apps.instagram.api import CommentTrigger

    account, user, conn = make_page()
    trigger = CommentTrigger.objects.create(
        account=account,
        name="Price",
        match_type=CommentTrigger.MatchType.KEYWORD,
        keywords="price",
        reply_template="Hi!",
        channels=RuleChannels.INSTAGRAM,
    )
    client.force_login(user)

    html = client.get("/instagram/rules/").content.decode()
    assert "1 rule only applies to Instagram comments" in html

    client.post("/instagram/rules/extend-to-facebook/")

    trigger.refresh_from_db()
    assert trigger.channels == RuleChannels.BOTH
    assert (
        "Apply to Facebook too" not in client.get("/instagram/rules/").content.decode()
    )


def test_an_extended_rule_sends_the_private_reply_automatically(client):
    from unittest.mock import patch

    from apps.facebook.models.message import FacebookOutboundMessage
    from apps.facebook.tests.helpers import FakeGraph, comment_payload, post_webhook
    from apps.instagram.api import CommentTrigger

    account, user, conn = make_page()
    CommentTrigger.objects.create(
        account=account,
        name="Price",
        match_type=CommentTrigger.MatchType.KEYWORD,
        keywords="price",
        reply_template="Hi! Sending details now.",
        channels=RuleChannels.INSTAGRAM,
    )
    client.force_login(user)
    client.post("/instagram/rules/extend-to-facebook/")

    graph = FakeGraph(recipient_psid="psid_dm")
    with (
        override_settings(WHATSAPP_APP_SECRET="fb-test-secret"),
        patch("apps.facebook.providers.meta.requests.request", graph),
    ):
        post_webhook(client, comment_payload(conn.page_id, "user_1", "What price?"))

    outbound = FacebookOutboundMessage.objects.get(
        action_type=FacebookOutboundMessage.ActionType.PRIVATE_REPLY
    )
    assert outbound.status == FacebookOutboundMessage.Status.SENT


def test_after_a_private_reply_the_inbox_says_to_wait_for_their_reply(client):
    from apps.core.actions import ActionError
    from apps.facebook.services import inbox
    from apps.facebook.services.outbound import AWAITING_REPLY_ERROR, dm_window
    from apps.facebook.tests.helpers import make_messenger_contact

    account, user, conn = make_page()
    # A private reply opened the thread; the customer hasn't written back.
    fb_contact, fb_convo, spine = make_messenger_contact(conn, last_inbound=None)

    with pytest.raises(ActionError, match="once they reply"):
        inbox.send(spine, "Anything else?", "inbox:abc")
    assert dm_window(account.pk, conn.page_id, fb_contact.psid) == "awaiting"
    assert "Messenger lets you write again" in AWAITING_REPLY_ERROR

    client.force_login(user)
    html = client.get(f"/inbox/{spine.public_id}/").content.decode()
    assert "sent a private reply to their comment" in html


class _GraphWithoutRecipient:
    """Meta answers the private reply without recipient_id; the message names it."""

    def __init__(self, psid):
        from apps.facebook.tests.helpers import FakeGraph

        self.psid = psid
        self.inner = FakeGraph()

    def __call__(self, method, url, json=None, params=None, timeout=None):
        from apps.facebook.tests.helpers import _Resp

        path = url.split(".com/", 1)[1].split("/", 1)[1]
        if method == "get" and path.startswith("m_"):
            return _Resp(200, {"to": {"data": [{"id": self.psid}]}})
        return self.inner(method, url, json=json, params=params, timeout=timeout)


def test_customer_reply_after_comment_to_dm_opens_the_reply_window(client):
    from unittest.mock import patch

    from apps.conversations.models import Conversation
    from apps.facebook.services import inbox
    from apps.facebook.tests.helpers import comment_payload, dm_payload, post_webhook
    from apps.instagram.api import CommentTrigger

    account, user, conn = make_page()
    CommentTrigger.objects.create(
        account=account,
        name="Price",
        match_type=CommentTrigger.MatchType.KEYWORD,
        keywords="price",
        reply_template="Hi! Sending details now.",
        channels=RuleChannels.FACEBOOK,
    )
    graph = _GraphWithoutRecipient("psid_customer")
    with (
        override_settings(WHATSAPP_APP_SECRET="fb-test-secret"),
        patch("apps.facebook.providers.meta.requests.request", graph),
        patch("apps.facebook.services.contacts._enrich_contact_profile"),
    ):
        post_webhook(client, comment_payload(conn.page_id, "asid_9", "What price?"))
        post_webhook(client, dm_payload(conn.page_id, "psid_customer", "Yes please"))

        spine = Conversation.objects.get(account=account, channel="facebook")
        assert spine.facebook_conversation.facebook_contact.psid == "psid_customer"
        assert spine.facebook_conversation.window_is_open
        inbox.send(spine, "Here you go", "inbox:k1")

    assert Conversation.objects.filter(account=account, channel="facebook").count() == 1

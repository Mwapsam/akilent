"""Facebook in the inbox: Messenger, Page comments, moderation, comment-to-DM and the
Comments view — the same behaviour as Instagram (apps/instagram/tests)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.conversations.models import Conversation, ConversationEntryPoint, Message
from apps.core.actions import ActionError, run_action
from apps.facebook.models import (
    FacebookComment,
    FacebookCommentThread,
    FacebookContact,
    FacebookMessage,
    FacebookOutboundMessage,
    FacebookWebhookEventLog,
)
from apps.instagram.models import CommentTrigger, ModerationLog, ModerationRule
from apps.instagram.models.moderation import RuleChannels

from .helpers import (
    APP_SECRET,
    FakeGraph,
    comment_payload,
    dm_payload,
    make_messenger_contact,
    make_page,
    post_webhook,
)

pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures("_signed"),
]

GRAPH = "apps.facebook.providers.meta.requests.request"


@pytest.fixture
def _signed():
    with override_settings(
        WHATSAPP_APP_SECRET=APP_SECRET,
        INSTAGRAM_APP_SECRET="",
        FACEBOOK_VERIFY_TOKEN="fb-verify",
    ):
        yield


@pytest.fixture
def graph():
    fake = FakeGraph()
    with patch(GRAPH, side_effect=fake):
        yield fake


# --- webhook ------------------------------------------------------------------------


def test_verify_handshake(client):
    ok = client.get(
        "/facebook/webhook/",
        {
            "hub.mode": "subscribe",
            "hub.verify_token": "fb-verify",
            "hub.challenge": "42",
        },
    )
    assert ok.status_code == 200 and ok.content == b"42"
    bad = client.get(
        "/facebook/webhook/",
        {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"},
    )
    assert bad.status_code == 403


def test_unsigned_post_is_refused(client):
    _account, _user, conn = make_page()
    response = client.post(
        "/facebook/webhook/",
        data=dm_payload(conn.page_id, "p1", "hi"),
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256="sha256=bad",
    )
    assert response.status_code == 403
    assert not FacebookWebhookEventLog.objects.exists()


def test_messenger_message_lands_in_the_inbox_once(client, graph):
    account, _user, conn = make_page()
    payload = dm_payload(conn.page_id, "psid-1", "Do you deliver?", mid="m_1")

    assert post_webhook(client, payload).status_code == 200
    post_webhook(client, payload)  # Meta redelivers

    fb_message = FacebookMessage.objects.get(message_id="m_1")
    assert fb_message.body == "Do you deliver?"
    spine = Conversation.objects.get(account=account)
    assert spine.channel == Conversation.Channel.FACEBOOK
    assert Message.objects.filter(conversation=spine).count() == 1
    assert spine.facebook_conversation.window_is_open
    contact = FacebookContact.objects.get(psid="psid-1")
    assert contact.name == "Profile Name"  # Business Asset User Profile Access
    assert contact.contact.first_name == "Profile"
    assert FacebookWebhookEventLog.objects.count() == 1


def test_a_non_page_object_is_ignored(client):
    _account, _user, conn = make_page()
    payload = {**dm_payload(conn.page_id, "p1", "hi"), "object": "instagram"}
    assert post_webhook(client, payload).status_code == 200
    assert not FacebookWebhookEventLog.objects.exists()


def test_without_the_facebook_feature_nothing_reaches_the_inbox(client, graph):
    account, _user, conn = make_page(inbox=False)
    post_webhook(client, dm_payload(conn.page_id, "p1", "hi"))
    assert not Conversation.objects.filter(account=account).exists()


def test_a_message_sent_from_the_page_inbox_is_recorded_as_ours(client, graph):
    account, _user, conn = make_page()
    post_webhook(client, dm_payload(conn.page_id, "psid-2", "Hello"))
    post_webhook(client, dm_payload(conn.page_id, "psid-2", "Yes we do", echo=True))
    out = Message.objects.get(
        conversation__account=account, direction=Message.Direction.OUTBOUND
    )
    assert out.body == "Yes we do"
    assert out.metadata["sent_by"] == "facebook_app"


def test_an_attachment_is_labelled_and_queued_for_download(client, graph):
    _account, _user, conn = make_page()
    with patch("apps.facebook.tasks.download_facebook_media.delay") as download:
        post_webhook(
            client,
            dm_payload(
                conn.page_id,
                "psid-3",
                "",
                attachments=[
                    {"type": "image", "payload": {"url": "https://cdn/x.jpg"}}
                ],
            ),
        )
    msg = FacebookMessage.objects.get()
    assert msg.body == "[Photo]" and msg.media_type == "image"
    download.assert_called_once_with(msg.pk)


# --- replies from the inbox -----------------------------------------------------------


def test_a_reply_inside_24_hours_is_a_response(graph):
    _account, user, conn = make_page()
    _c, _convo, spine = make_messenger_contact(conn)
    result = run_action("reply", {}, conversation=spine, body="On its way")
    assert result["outbound_message_id"]
    _m, path, body, _p = graph.sent()[0]
    assert (
        body["messaging_type"] == "RESPONSE" and body["message"]["text"] == "On its way"
    )


def test_after_24_hours_only_a_person_can_reply_with_the_human_agent_tag(graph):
    from apps.conversations.inbox_keys import inbox_key

    _account, user, conn = make_page()
    _c, _convo, spine = make_messenger_contact(conn, last_inbound=timedelta(days=2))

    with pytest.raises(ActionError, match="24 hours"):
        run_action(
            "reply", {}, conversation=spine, body="Automated", idempotency_key="wf:1"
        )
    assert not graph.sent()

    key = inbox_key(spine, user, "abcdef12-key")
    run_action("reply", {}, conversation=spine, body="Typed", idempotency_key=key)
    _m, _path, body, _p = graph.sent()[0]
    assert body["messaging_type"] == "MESSAGE_TAG" and body["tag"] == "HUMAN_AGENT"
    assert FacebookOutboundMessage.objects.get(idempotency_key=key).human_agent


def test_after_7_days_nobody_can_reply(graph):
    from apps.conversations.inbox_keys import inbox_key

    _account, user, conn = make_page()
    _c, _convo, spine = make_messenger_contact(conn, last_inbound=timedelta(days=8))
    with pytest.raises(ActionError, match="7 days"):
        run_action(
            "reply",
            {},
            conversation=spine,
            body="Typed",
            idempotency_key=inbox_key(spine, user, "abcdef12-old"),
        )
    assert not graph.sent()


def test_automation_window_check_stays_24_hours():
    from apps.conversations.api import conversation_window_is_open

    _account, _user, conn = make_page()
    _c, _convo, spine = make_messenger_contact(conn, last_inbound=timedelta(days=2))
    assert not conversation_window_is_open(spine)


def test_a_reply_after_the_page_disconnects_fails_clearly(graph):
    from apps.facebook.services.pages import disconnect_page

    _account, _user, conn = make_page()
    _c, _convo, spine = make_messenger_contact(conn)
    disconnect_page(conn, unsubscribe=False)
    with pytest.raises(ActionError, match="reconnect"):
        run_action("reply", {}, conversation=spine, body="Hi")


# --- comments: moderation and comment-to-DM -------------------------------------------


def _rule(account, channels, **kw):
    defaults = {
        "name": "Spam",
        "match_type": ModerationRule.MatchType.KEYWORD,
        "keywords": "crypto",
        "moderation_action": ModerationRule.ModerationAction.HIDE,
    }
    return ModerationRule.objects.create(
        account=account, channels=channels, **{**defaults, **kw}
    )


def test_a_page_comment_is_stored_and_moderated_by_a_facebook_rule(client, graph):
    account, _user, conn = make_page()
    _rule(account, RuleChannels.FACEBOOK)
    post_webhook(client, comment_payload(conn.page_id, "u1", "Earn crypto now"))

    comment = FacebookComment.objects.get()
    assert comment.moderation_state == FacebookComment.ModerationState.HIDDEN
    hide = [c for c in graph.calls if c[3].get("is_hidden") == "true"]
    assert hide and hide[0][1] == comment.comment_id
    log = ModerationLog.objects.get()
    assert log.facebook_comment == comment and log.comment is None


def test_an_instagram_only_rule_leaves_facebook_comments_alone(client, graph):
    account, _user, conn = make_page()
    _rule(account, RuleChannels.INSTAGRAM)
    post_webhook(client, comment_payload(conn.page_id, "u1", "Earn crypto now"))
    assert FacebookComment.objects.get().moderation_state == "pending"
    assert not ModerationLog.objects.exists()


def test_the_pages_own_comment_is_skipped(client, graph):
    _account, _user, conn = make_page()
    post_webhook(client, comment_payload(conn.page_id, conn.page_id, "Thanks all"))
    assert not FacebookCommentThread.objects.exists()


def test_a_keyword_comment_gets_one_private_reply_and_opens_messenger(client):
    from apps.publishing.models import ContentPost, ContentPostVariant

    account, _user, conn = make_page()
    CommentTrigger.objects.create(
        account=account,
        name="Price",
        match_type=CommentTrigger.MatchType.KEYWORD,
        keywords="price",
        reply_template="Hi {username}, sending it now!",
        channels=RuleChannels.BOTH,
    )
    post_id = f"{conn.page_id}_777"
    post = ContentPost.objects.create(account=account, caption="New stock")
    ContentPostVariant.objects.create(
        post=post, platform="facebook", publication_id="777", caption="New stock"
    )
    payload = comment_payload(
        conn.page_id,
        "commenter-1",
        "price please?",
        post_id=post_id,
        name="Grace Hopper",
    )
    fake = FakeGraph(recipient_psid="psid-of-grace")
    with patch(GRAPH, side_effect=fake):
        post_webhook(client, payload)
        post_webhook(client, payload)  # redelivery: still one reply

    replies = fake.sent()
    assert len(replies) == 1
    _m, _path, body, _p = replies[0]
    comment_id = payload["entry"][0]["changes"][0]["value"]["comment_id"]
    assert body["recipient"] == {"comment_id": comment_id}
    assert body["message"]["text"] == "Hi Grace, sending it now!"

    thread = FacebookCommentThread.objects.get()
    assert thread.trigger_fired_at and thread.conversation_id
    messenger = FacebookContact.objects.get(psid="psid-of-grace")
    commenter = FacebookContact.objects.get(psid="commenter-1")
    assert messenger.contact_id == commenter.contact_id  # one customer
    assert thread.conversation.facebook_conversation.facebook_contact == messenger
    assert (
        ConversationEntryPoint.objects.get(conversation=thread.conversation).kind
        == ConversationEntryPoint.Kind.FB_COMMENT
    )


def test_a_reply_to_a_comment_never_fires_a_trigger(client, graph):
    account, _user, conn = make_page()
    CommentTrigger.objects.create(
        account=account,
        name="Any",
        match_type=CommentTrigger.MatchType.ANY_COMMENT,
        reply_template="Hi",
    )
    root = comment_payload(conn.page_id, "u1", "nice")
    value = root["entry"][0]["changes"][0]["value"]
    post_webhook(client, root)
    graph.calls.clear()
    post_webhook(
        client,
        comment_payload(
            conn.page_id,
            "u2",
            "agreed",
            post_id=value["post_id"],
            parent_id=value["comment_id"],
        ),
    )
    assert not graph.sent()
    assert FacebookCommentThread.objects.count() == 1
    assert FacebookComment.objects.count() == 2


def test_a_comment_removed_on_facebook_is_mirrored(client, graph):
    _account, _user, conn = make_page()
    payload = comment_payload(conn.page_id, "u1", "hello")
    post_webhook(client, payload)
    value = payload["entry"][0]["changes"][0]["value"]
    post_webhook(
        client,
        comment_payload(
            conn.page_id,
            "u1",
            "",
            comment_id=value["comment_id"],
            post_id=value["post_id"],
            verb="remove",
        ),
    )
    assert FacebookComment.objects.get().moderation_state == "deleted"


# --- the Comments view (Instagram and Facebook) --------------------------------------


def test_comments_view_comments_replies_and_moderates(client, graph):
    from apps.conversations import comments

    account, user, conn = make_page()
    post_webhook(client, comment_payload(conn.page_id, "u1", "Is this in blue?"))
    comment = FacebookComment.objects.get()

    client.force_login(user)
    page = client.get("/inbox/comments/")
    assert page.status_code == 200 and "Is this in blue?" in page.content.decode()

    comments.post_comment(
        account, "facebook", comment.thread.post_id, "Restocked today!", user, "k1"
    )
    comments.reply(account, "facebook", comment.pk, "Yes, in blue", user, "k2")
    comments.moderate(account, "facebook", comment.pk, "hide", user)
    comments.moderate(account, "facebook", comment.pk, "unhide", user)

    posted = graph.sent("/comments")
    assert [c[1] for c in posted] == [
        f"{comment.thread.post_id}/comments",
        f"{comment.thread.comment_id}/comments",
    ]
    assert FacebookComment.objects.filter(direction="outbound").count() == 1
    logs = ModerationLog.objects.filter(facebook_comment=comment)
    assert [log.moderation_action for log in logs.order_by("pk")] == ["hide", "unhide"]
    assert all(log.acted_by == user for log in logs)
    comment.refresh_from_db()
    assert not comment.is_hidden
    # A manual public reply stays out of the DM inbox.
    assert not Conversation.objects.filter(account=account).exists()


def test_a_private_reply_from_the_comments_view_opens_the_conversation(client):
    from apps.conversations import comments

    account, user, conn = make_page()
    with patch(GRAPH, side_effect=FakeGraph()):
        post_webhook(client, comment_payload(conn.page_id, "u1", "how much?"))
    comment = FacebookComment.objects.get()
    with patch(GRAPH, side_effect=FakeGraph(recipient_psid="psid-u1")):
        conversation = comments.private_reply(
            account, "facebook", comment.pk, "It's 20 ZMW", user, "k3"
        )
    assert conversation is not None
    assert conversation.channel == Conversation.Channel.FACEBOOK
    assert Message.objects.filter(
        conversation=conversation, body="It's 20 ZMW"
    ).exists()


def test_comments_view_refuses_a_channel_the_business_cant_use(graph):
    from apps.conversations import comments

    account, user, _conn = make_page(inbox=False)
    with pytest.raises(comments.CommentError):
        comments.post_comment(account, "facebook", "p_1", "hi", user, "k")


# --- connecting ---------------------------------------------------------------------


def test_connecting_a_page_subscribes_it_and_adds_facebook_rules(graph):
    from apps.facebook.services.pages import on_page_connected, subscribe_page

    account, _user, conn = make_page()
    conn.webhook_subscribed_at = None
    conn.save()
    on_page_connected(conn)
    conn.refresh_from_db()
    assert conn.webhook_subscribed_at is not None
    sub = next(c for c in graph.calls if c[1].endswith("subscribed_apps"))
    assert (
        "feed" in sub[3]["subscribed_fields"]
        and "messages" in sub[3]["subscribed_fields"]
    )
    assert ModerationRule.objects.filter(
        account=account, channels=RuleChannels.FACEBOOK
    ).exists()

    with patch(GRAPH, side_effect=FakeGraph(fail={"subscribed_apps": (200, "No")})):
        assert not subscribe_page(conn)
    conn.refresh_from_db()
    assert conn.webhook_subscribed_at is None and conn.webhook_error

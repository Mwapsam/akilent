"""Regression tests for the Instagram messaging fixes.

Each test names the behaviour it protects: non-message events, echoes, the
business's own comments, per-message webhook idempotency, routing, workflow
replies, the reply action, the outbox drain, token handling, the 24h window,
and disconnect / deauthorize.
"""

import base64
import hashlib
import hmac
import json
import secrets
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.instagram.providers.base import SendResult
from apps.instagram.tests.helpers import (
    comment_payload,
    dm_payload,
    make_account,
    make_instagram_account,
    make_instagram_contact,
    signed_post,
)
from apps.instagram.views import InstagramWebhookView

AUTOMATION_ON = "apps.instagram.services.inbound._automation_events_enabled"


def _deliver(payload):
    """POST a payload through the real webhook view, then process it synchronously."""
    from apps.instagram.models.webhook import WebhookEventLog
    from apps.instagram.tasks import process_instagram_event

    before = set(WebhookEventLog.objects.values_list("pk", flat=True))
    with patch("apps.instagram.views.process_instagram_event"):
        response = InstagramWebhookView.as_view()(signed_post(payload))
    for event in WebhookEventLog.objects.exclude(pk__in=before):
        process_instagram_event(event.pk)
    return response


def _messaging_payload(entry_id, item, *, time=None):
    return {
        "object": "instagram",
        "entry": [
            {
                "id": entry_id,
                "time": time or int(timezone.now().timestamp()),
                "messaging": [item],
            }
        ],
    }


class InstagramCase(TestCase):
    def setUp(self):
        self.account, self.user = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.igsid = f"igsid_{secrets.token_hex(4)}"

    def dm(self, body, **kw):
        return dm_payload(self.ig_account.page_id, self.igsid, body, **kw)


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------


class TestNonMessageEvents(InstagramCase):
    def test_read_receipts_and_reactions_create_no_message(self):
        from apps.conversations.models import Message
        from apps.instagram.models import InstagramMessage

        now_ms = int(timezone.now().timestamp() * 1000)
        for extra in (
            {"read": {"mid": "mid.seen"}},
            {"reaction": {"mid": "mid.x", "action": "react", "emoji": "❤"}},
            {"postback": {"payload": "GET_STARTED"}},
        ):
            _deliver(
                _messaging_payload(
                    self.ig_account.page_id,
                    {
                        "sender": {"id": self.igsid},
                        "recipient": {"id": self.ig_account.page_id},
                        "timestamp": now_ms,
                        **extra,
                    },
                )
            )
        self.assertEqual(InstagramMessage.objects.count(), 0)
        self.assertEqual(Message.objects.count(), 0)


class TestEchoes(InstagramCase):
    def _echo(self, mid, text):
        return _messaging_payload(
            self.ig_account.page_id,
            {
                "sender": {"id": self.ig_account.instagram_business_account_id},
                "recipient": {"id": self.igsid},
                "timestamp": int(timezone.now().timestamp() * 1000),
                "message": {"mid": mid, "text": text, "is_echo": True},
            },
        )

    def test_echo_of_a_phone_app_reply_appears_once_as_outbound(self):
        from apps.conversations.models import Message

        _deliver(self.dm("Hi, is this available?"))
        _deliver(self._echo("mid.phone1", "Yes it is!"))
        _deliver(self._echo("mid.phone1", "Yes it is!"))  # Meta redelivery

        outbound = Message.objects.filter(direction="outbound")
        self.assertEqual(outbound.count(), 1)
        self.assertEqual(outbound.get().body, "Yes it is!")
        self.assertEqual(outbound.get().metadata.get("sent_by"), "instagram_app")
        self.assertEqual(Message.objects.filter(direction="inbound").count(), 1)

    def test_echo_of_an_akilent_send_is_not_duplicated(self):
        from apps.conversations.actions import run_action
        from apps.conversations.models import Conversation, Message

        _deliver(self.dm("Hello"))
        conversation = Conversation.objects.get(channel="instagram")
        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider.send_message",
            return_value=SendResult(success=True, provider_message_id="mid.akilent1"),
        ):
            run_action(
                "reply",
                {"account": self.account},
                conversation=conversation,
                body="Hi!",
            )
        _deliver(self._echo("mid.akilent1", "Hi!"))

        self.assertEqual(Message.objects.filter(direction="outbound").count(), 1)


class TestOwnComments(InstagramCase):
    def test_business_own_comment_is_ignored(self):
        from apps.instagram.models import CommentThread, InstagramContact

        _deliver(
            comment_payload(
                self.ig_account.page_id,
                self.ig_account.instagram_business_account_id,
                "Thanks everyone! DM us for prices",
            )
        )
        self.assertEqual(CommentThread.objects.count(), 0)
        self.assertEqual(InstagramContact.objects.count(), 0)


class TestWebhookIdempotencyPerMessage(InstagramCase):
    def test_two_messages_in_the_same_second_are_both_kept(self):
        from apps.instagram.models import InstagramMessage

        ts = int(timezone.now().timestamp() * 1000)
        _deliver(self.dm("first", msg_id="mid.a", ts=ts))
        _deliver(self.dm("second", msg_id="mid.b", ts=ts))
        self.assertEqual(
            set(InstagramMessage.objects.values_list("body", flat=True)),
            {"first", "second"},
        )

    def test_redelivery_of_the_same_message_is_one_event(self):
        from apps.instagram.models.webhook import WebhookEventLog

        payload = self.dm("hello", msg_id="mid.same")
        _deliver(payload)
        _deliver(payload)
        self.assertEqual(WebhookEventLog.objects.count(), 1)


class TestMessageTypes(InstagramCase):
    def test_story_reply_and_photo_are_labelled(self):
        from apps.instagram.services.inbound import describe_message

        self.assertEqual(
            describe_message({"text": "love it", "reply_to": {"story": {"id": "1"}}}),
            ("story_reply", "[Replied to your story] love it"),
        )
        self.assertEqual(
            describe_message({"attachments": [{"type": "image"}]}), ("image", "[Photo]")
        )
        self.assertEqual(describe_message({"text": "hi"}), ("text", "hi"))


# ---------------------------------------------------------------------------
# Spine parity with WhatsApp
# ---------------------------------------------------------------------------


class TestRouting(InstagramCase):
    def test_first_dm_routes_the_new_conversation_once(self):
        with patch("apps.conversations.services.run_action") as run:
            _deliver(self.dm("hello", msg_id="mid.r1"))
            _deliver(self.dm("again", msg_id="mid.r2"))
        routed = [c for c in run.call_args_list if c.args[0] == "route_conversation"]
        self.assertEqual(len(routed), 1)


@patch(AUTOMATION_ON, return_value=True)
class TestWorkflowsAndAi(InstagramCase):
    def test_wait_for_reply_resumes_from_an_instagram_answer(self, automation_switch):
        with (
            patch(
                "apps.automation.workflow_engine.resume_on_reply", return_value=True
            ) as resume,
            patch("apps.automation.workflow_engine.enroll_for_trigger") as enroll,
        ):
            _deliver(self.dm("Prices"))
        resume.assert_called_once()
        self.assertEqual(resume.call_args.args[2]["body"], "Prices")
        enroll.assert_not_called()

    def test_ai_is_told_when_automation_already_answered(self, automation_switch):
        from apps.conversations.signals import conversation_message_processed

        seen = []

        def receiver(sender, handled_by_automation=False, **kw):
            seen.append(handled_by_automation)

        conversation_message_processed.connect(receiver)
        try:
            with patch(
                "apps.automation.workflow_engine.enroll_for_trigger", return_value=1
            ):
                _deliver(self.dm("hello"))
        finally:
            conversation_message_processed.disconnect(receiver)
        self.assertEqual(seen, [True])


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------


class TestReplyAction(InstagramCase):
    def test_reply_is_linked_and_does_not_reopen_the_window(self):
        from apps.conversations.actions import run_action
        from apps.conversations.models import Conversation, Message
        from apps.instagram.models import InstagramConversation, InstagramMessage

        _deliver(self.dm("Hello"))
        conversation = Conversation.objects.get(channel="instagram")
        ig_convo = InstagramConversation.objects.get()
        ig_convo.last_inbound_at = timezone.now() - timedelta(hours=23)
        ig_convo.save(update_fields=["last_inbound_at"])

        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider.send_message",
            return_value=SendResult(success=True, provider_message_id="mid.out1"),
        ):
            result = run_action(
                "reply",
                {"account": self.account},
                conversation=conversation,
                body="Hi!",
            )

        msg = Message.objects.get(pk=result["outbound_message_id"])
        self.assertEqual(msg.direction, "outbound")
        self.assertEqual(msg.instagram_message.message_id, "mid.out1")
        self.assertEqual(
            InstagramMessage.objects.get(message_id="mid.out1").direction, "outbound"
        )
        ig_convo.refresh_from_db()
        # A business reply must not push the customer's 24h window forward.
        self.assertLess(ig_convo.last_inbound_at, timezone.now() - timedelta(hours=22))


class TestWindow(InstagramCase):
    def test_window_closes_24_hours_after_the_customer_last_wrote(self):
        from apps.conversations.api import conversation_window_is_open
        from apps.conversations.models import Conversation
        from apps.instagram.models import InstagramConversation

        _deliver(self.dm("Hello"))
        conversation = Conversation.objects.get(channel="instagram")
        self.assertTrue(conversation_window_is_open(conversation))

        InstagramConversation.objects.update(
            last_inbound_at=timezone.now() - timedelta(hours=25)
        )
        self.assertFalse(conversation_window_is_open(conversation))


class TestOutbox(InstagramCase):
    def _queued(self):
        from apps.instagram.models import OutboundMessage
        from apps.instagram.services.outbound import enqueue_reply

        make_instagram_contact(self.account, self.ig_account, igsid=self.igsid)
        return enqueue_reply(
            self.ig_account,
            self.igsid,
            "Your order is ready",
            action_type=OutboundMessage.ActionType.DM_REPLY,
            idempotency_key=f"wf:{secrets.token_hex(4)}",
        )

    def test_transient_failure_is_retried_by_the_drain(self):
        from apps.conversations.models import Message
        from apps.instagram.models import OutboundMessage
        from apps.instagram.services.outbound import drain_outbox, send_outbound

        outbound = self._queued()
        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider.send_message",
            return_value=SendResult(
                success=False, error="[2] temporary", terminal=False
            ),
        ):
            self.assertFalse(send_outbound(outbound))
        outbound.refresh_from_db()
        self.assertEqual(outbound.status, OutboundMessage.Status.QUEUED)

        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider.send_message",
            return_value=SendResult(success=True, provider_message_id="mid.retry"),
        ):
            result = drain_outbox(now=timezone.now() + timedelta(hours=1))

        self.assertEqual(result["sent"], 1)
        outbound.refresh_from_db()
        self.assertEqual(outbound.status, OutboundMessage.Status.SENT)
        sent = Message.objects.get(direction="outbound")
        self.assertEqual(sent.metadata.get("sent_by"), "automation")

    def test_invalid_token_marks_the_account_for_reconnect(self):
        from apps.instagram.services.outbound import send_outbound

        outbound = self._queued()
        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider.send_message",
            return_value=SendResult(
                success=False, error="[190] expired", terminal=True, error_code=190
            ),
        ):
            send_outbound(outbound)
        self.ig_account.refresh_from_db()
        self.assertTrue(self.ig_account.token_expired)

    def test_interrupted_send_is_marked_unconfirmed_not_retried(self):
        from apps.instagram.models import OutboundMessage
        from apps.instagram.services.outbound import drain_outbox

        outbound = self._queued()
        OutboundMessage.objects.filter(pk=outbound.pk).update(
            status=OutboundMessage.Status.SENDING,
            updated_at=timezone.now() - timedelta(minutes=30),
        )
        drain_outbox()
        outbound.refresh_from_db()
        self.assertEqual(outbound.status, OutboundMessage.Status.UNCONFIRMED)


# ---------------------------------------------------------------------------
# Tokens + connection lifecycle
# ---------------------------------------------------------------------------


class TestTokenRefresh(InstagramCase):
    def test_token_near_expiry_is_refreshed(self):
        from apps.instagram.tasks import refresh_instagram_tokens

        self.ig_account.token_expires_at = timezone.now() + timedelta(days=3)
        self.ig_account.save()
        with patch(
            "apps.instagram.oauth.refresh_long_lived_token",
            return_value=("new_token", 5184000),
        ):
            result = refresh_instagram_tokens()
        self.assertEqual(result["refreshed"], 1)
        self.ig_account.refresh_from_db()
        self.assertEqual(self.ig_account.access_token, "new_token")
        self.assertGreater(
            self.ig_account.token_expires_at, timezone.now() + timedelta(days=59)
        )

    def test_connection_without_recorded_expiry_is_refreshed_once_old_enough(self):
        from apps.instagram.models import InstagramBusinessAccount
        from apps.instagram.tasks import refresh_instagram_tokens

        InstagramBusinessAccount.objects.filter(pk=self.ig_account.pk).update(
            access_token="IGAA_old",
            token_expires_at=None,
            updated_at=timezone.now() - timedelta(days=2),
        )
        with patch(
            "apps.instagram.oauth.refresh_long_lived_token",
            return_value=("IGAA_new", 5184000),
        ):
            refresh_instagram_tokens()
        self.ig_account.refresh_from_db()
        self.assertEqual(self.ig_account.access_token, "IGAA_new")
        self.assertIsNotNone(self.ig_account.token_expires_at)

    def test_permanent_operator_token_is_left_alone(self):
        from apps.instagram.models import InstagramBusinessAccount
        from apps.instagram.tasks import refresh_instagram_tokens

        InstagramBusinessAccount.objects.filter(pk=self.ig_account.pk).update(
            access_token="EAA_system_user",
            token_expires_at=None,
            updated_at=timezone.now() - timedelta(days=2),
        )
        with patch("apps.instagram.oauth.refresh_long_lived_token") as refresh:
            refresh_instagram_tokens()
        refresh.assert_not_called()
        self.ig_account.refresh_from_db()
        self.assertFalse(self.ig_account.token_expired)

    def test_refused_refresh_asks_for_reconnect(self):
        from apps.instagram.oauth import InstagramOAuthError
        from apps.instagram.tasks import refresh_instagram_tokens

        self.ig_account.token_expires_at = timezone.now() + timedelta(days=1)
        self.ig_account.save()
        with patch(
            "apps.instagram.oauth.refresh_long_lived_token",
            side_effect=InstagramOAuthError("Session has expired"),
        ):
            refresh_instagram_tokens()
        self.ig_account.refresh_from_db()
        self.assertTrue(self.ig_account.token_expired)


class TestDisconnect(InstagramCase):
    def test_disconnect_keeps_history_and_stops_webhooks(self):
        from apps.conversations.models import Message
        from apps.instagram.views import disconnect_instagram_account

        _deliver(self.dm("Hello"))
        with patch(
            "apps.instagram.oauth.unsubscribe_ig_account_from_webhooks",
            return_value=True,
        ) as unsubscribe:
            disconnect_instagram_account(self.ig_account, unsubscribe=True)
        unsubscribe.assert_called_once()
        self.ig_account.refresh_from_db()
        self.assertFalse(self.ig_account.is_active)
        self.assertIsNone(self.ig_account.access_token)
        self.assertEqual(Message.objects.count(), 1)


@override_settings(INSTAGRAM_APP_SECRET="ig_secret")
class TestDeauthorize(InstagramCase):
    def _signed(self, payload: dict, secret="ig_secret") -> str:
        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        encoded = b64(json.dumps(payload).encode())
        sig = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        return f"{b64(sig)}.{encoded}"

    def test_deauthorize_disconnects_the_account(self):
        from apps.instagram.views import instagram_deauthorize

        request = RequestFactory().post(
            "/instagram/deauthorize/",
            {
                "signed_request": self._signed(
                    {"user_id": self.ig_account.instagram_business_account_id}
                )
            },
        )
        self.assertEqual(instagram_deauthorize(request).status_code, 200)
        self.ig_account.refresh_from_db()
        self.assertFalse(self.ig_account.is_active)

    def test_forged_signed_request_is_rejected(self):
        from apps.instagram.views import instagram_deauthorize

        request = RequestFactory().post(
            "/instagram/deauthorize/",
            {
                "signed_request": self._signed(
                    {"user_id": self.ig_account.instagram_business_account_id},
                    secret="wrong",
                )
            },
        )
        self.assertEqual(instagram_deauthorize(request).status_code, 400)
        self.ig_account.refresh_from_db()
        self.assertTrue(self.ig_account.is_active)

    def test_data_deletion_returns_confirmation(self):
        from apps.instagram.views import instagram_data_deletion

        request = RequestFactory().post(
            "/instagram/data-deletion/",
            {
                "signed_request": self._signed(
                    {"user_id": self.ig_account.instagram_business_account_id}
                )
            },
        )
        with patch("apps.instagram.tasks.delete_instagram_account_data") as task:
            task.delay = MagicMock()
            with self.captureOnCommitCallbacks(execute=True):
                response = instagram_data_deletion(request)
        body = json.loads(response.content)
        self.assertIn("confirmation_code", body)
        self.assertIn(body["confirmation_code"], body["url"])
        task.delay.assert_called_once()

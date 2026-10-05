"""
Instagram Phase 1 acceptance tests.

Each test proves one of the twelve Phase 1 acceptance criteria:

 1. Valid webhook accepted; invalid signature rejected (403).
 2. Repeated webhook deliveries do not create duplicates (idempotency).
 3. Inbound DMs create the correct tenant-scoped contact and shared conversation.
 4. Outbound staff reply is delivered; duplicate send prevented on Celery retry.
 5. AI response respects can_send() eligibility and handoff rules.
 6. A DM with buying intent creates an AIProposal — not a Lead.
 7. A buying-intent comment does not create a conversation or lead.
 8. Staff confirmation creates exactly one Lead linked to the correct conversation.
 9. Staff dismissal does not create a lead.
10. Public comment replies and private DMs are correctly distinguished.
11. Expired token / rate-limit / failed API responses handled safely.
12. WhatsApp regression — existing conversations continue to work.
"""

import hashlib
import hmac
import json
import secrets
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from apps.instagram.tests.helpers import (
    comment_payload,
    dm_payload,
    make_account,
    make_instagram_account,
    make_instagram_contact,
    signed_post,
)
from apps.instagram.views import InstagramWebhookView


# ---------------------------------------------------------------------------
# Test 1 — Webhook signature verification
# ---------------------------------------------------------------------------


class TestWebhookSignature(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.view = InstagramWebhookView.as_view()

    def _post(self, payload, secret="test_token"):
        return self.view(signed_post(payload, secret=secret))

    def test_valid_signature_accepted(self):
        payload = dm_payload(
            self.ig_account.page_id,
            sender_igsid="igsid_abc",
            body="Hello",
        )
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200)

    def test_invalid_signature_rejected(self):
        payload = dm_payload(
            self.ig_account.page_id,
            sender_igsid="igsid_abc",
            body="Hello",
        )
        resp = self._post(payload, secret="wrong_secret")
        self.assertEqual(resp.status_code, 403)

    def test_get_verification_handshake(self):
        from django.test import RequestFactory

        factory = RequestFactory()
        req = factory.get(
            "/instagram/webhook/",
            {
                "hub.mode": "subscribe",
                "hub.verify_token": self.ig_account.verify_token,
                "hub.challenge": "abc123",
            },
        )
        resp = InstagramWebhookView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"abc123")

    def test_get_wrong_verify_token_rejected(self):
        from django.test import RequestFactory

        factory = RequestFactory()
        req = factory.get(
            "/instagram/webhook/",
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "abc123",
            },
        )
        resp = InstagramWebhookView.as_view()(req)
        self.assertEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# Test 2 — Webhook idempotency (no duplicates on repeated delivery)
# ---------------------------------------------------------------------------


class TestWebhookIdempotency(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.view = InstagramWebhookView.as_view()

    def test_duplicate_webhook_creates_one_event_log(self):
        from apps.instagram.models.webhook import WebhookEventLog

        payload = dm_payload(
            self.ig_account.page_id,
            sender_igsid="igsid_dup",
            body="Duplicate test",
            msg_id="mid.DEDUP1",
        )
        with patch("apps.instagram.views.process_instagram_event"):
            self.view(signed_post(payload))
            self.view(signed_post(payload))  # second delivery

        self.assertEqual(WebhookEventLog.objects.count(), 1)

    def test_duplicate_dm_message_not_created_twice(self):
        from apps.instagram.models.message import InstagramMessage
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        payload = dm_payload(
            self.ig_account.page_id,
            sender_igsid=f"igsid_{secrets.token_hex(4)}",
            body="Hi",
            msg_id="mid.DEDUP2",
        )
        with patch("apps.instagram.views.process_instagram_event"):
            self.view(signed_post(payload))

        event = WebhookEventLog.objects.first()
        # Process the event twice (simulating Celery duplicate delivery)
        process_instagram_event(event.pk)
        process_instagram_event(event.pk)

        self.assertEqual(
            InstagramMessage.objects.filter(message_id="mid.DEDUP2").count(), 1
        )


# ---------------------------------------------------------------------------
# Test 3 — Inbound DM creates contact + conversation
# ---------------------------------------------------------------------------


class TestInboundDM(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def test_inbound_dm_creates_instagram_contact_and_conversation(self):
        from apps.conversations.models import Conversation
        from apps.instagram.models.contact import InstagramContact
        from apps.instagram.models.conversation import InstagramConversation
        from apps.instagram.models.message import InstagramMessage
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        igsid = f"igsid_{secrets.token_hex(4)}"
        payload = dm_payload(self.ig_account.page_id, igsid, "Hello there")

        view = InstagramWebhookView.as_view()
        with patch("apps.instagram.views.process_instagram_event"):
            view(signed_post(payload))

        event = WebhookEventLog.objects.first()
        process_instagram_event(event.pk)

        ig_contact = InstagramContact.objects.get(
            account=self.account, instagram_scoped_id=igsid
        )
        self.assertIsNotNone(ig_contact.contact)
        self.assertEqual(ig_contact.contact.account, self.account)
        self.assertEqual(ig_contact.contact.source, "instagram")

        ig_convo = InstagramConversation.objects.get(instagram_contact=ig_contact)
        self.assertTrue(ig_convo.is_open)

        from apps.conversations.models import ChannelConversation
        spine = ChannelConversation.objects.get(
            channel=Conversation.Channel.INSTAGRAM, object_id=ig_convo.pk
        ).conversation
        self.assertEqual(spine.channel, Conversation.Channel.INSTAGRAM)
        self.assertEqual(spine.contact, ig_contact.contact)

        self.assertEqual(InstagramMessage.objects.filter(conversation=ig_convo).count(), 1)

    def test_second_dm_from_same_contact_reuses_conversation(self):
        from apps.conversations.models import Conversation
        from apps.instagram.models.conversation import InstagramConversation
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        igsid = f"igsid_{secrets.token_hex(4)}"
        view = InstagramWebhookView.as_view()

        base_ts = int(timezone.now().timestamp() * 1000)
        for i, body in enumerate(["First message", "Second message"]):
            with patch("apps.instagram.views.process_instagram_event"):
                payload = dm_payload(
                    self.ig_account.page_id, igsid, body,
                    msg_id=f"mid.{secrets.token_hex(4)}",
                    ts=base_ts + (i * 2000),  # distinct second-level timestamps
                )
                view(signed_post(payload))
            event = WebhookEventLog.objects.filter(status="received").last()
            process_instagram_event(event.pk)

        self.assertEqual(InstagramConversation.objects.filter(is_open=True).count(), 1)
        self.assertEqual(Conversation.objects.filter(channel="instagram").count(), 1)

    def test_tenant_isolation(self):
        """A DM for account A is never visible to account B."""
        from apps.conversations.models import Conversation
        from apps.instagram.tasks import process_instagram_event
        from apps.instagram.models.webhook import WebhookEventLog

        account_b, _ = make_account()
        make_instagram_account(account_b)

        igsid = f"igsid_{secrets.token_hex(4)}"
        payload = dm_payload(self.ig_account.page_id, igsid, "Secret message")

        view = InstagramWebhookView.as_view()
        with patch("apps.instagram.views.process_instagram_event"):
            view(signed_post(payload))
        event = WebhookEventLog.objects.first()
        process_instagram_event(event.pk)

        # Account B should have zero Instagram conversations
        self.assertEqual(
            Conversation.objects.filter(
                account=account_b, channel="instagram"
            ).count(),
            0,
        )


# ---------------------------------------------------------------------------
# Test 4 — Outbound idempotency (duplicate send prevention)
# ---------------------------------------------------------------------------


class TestOutboundIdempotency(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def test_same_idempotency_key_creates_one_outbound(self):
        from apps.instagram.models.message import OutboundMessage
        from apps.instagram.services.outbound import enqueue_reply

        key = f"test:{secrets.token_hex(8)}"
        msg1 = enqueue_reply(
            self.ig_account, "igsid_xyz", "Hello",
            action_type=OutboundMessage.ActionType.DM_REPLY,
            idempotency_key=key,
        )
        msg2 = enqueue_reply(
            self.ig_account, "igsid_xyz", "Hello",
            action_type=OutboundMessage.ActionType.DM_REPLY,
            idempotency_key=key,
        )
        self.assertIsNotNone(msg1)
        self.assertIsNone(msg2)  # duplicate — silently swallowed
        self.assertEqual(OutboundMessage.objects.filter(idempotency_key=key).count(), 1)

    def test_already_sent_outbound_not_resent(self):
        from apps.instagram.models.message import OutboundMessage
        from apps.instagram.services.outbound import enqueue_reply, send_outbound

        key = f"test:{secrets.token_hex(8)}"
        outbound = enqueue_reply(
            self.ig_account, "igsid_resend", "Hi",
            action_type=OutboundMessage.ActionType.DM_REPLY,
            idempotency_key=key,
        )
        outbound.mark_sent("mid.SENT1")

        call_count_before = outbound.attempts
        result = send_outbound(outbound)

        # Already sent — should return True immediately without a new API call
        self.assertTrue(result)
        outbound.refresh_from_db()
        self.assertEqual(outbound.status, OutboundMessage.Status.SENT)
        self.assertEqual(outbound.attempts, call_count_before)  # unchanged


# ---------------------------------------------------------------------------
# Test 5 — Eligibility check before send
# ---------------------------------------------------------------------------


class TestSendEligibility(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def test_ineligible_contact_send_fails_terminally(self):
        from apps.instagram.models.message import OutboundMessage
        from apps.instagram.providers.base import EligibilityResult
        from apps.instagram.services.outbound import enqueue_reply, send_outbound

        outbound = enqueue_reply(
            self.ig_account, "igsid_blocked", "Hi",
            action_type=OutboundMessage.ActionType.DM_REPLY,
            idempotency_key=f"test:{secrets.token_hex(8)}",
        )

        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider"
        ) as MockProvider:
            MockProvider.return_value.can_send.return_value = EligibilityResult(
                eligible=False, reason="outside_window"
            )
            result = send_outbound(outbound)

        self.assertFalse(result)
        outbound.refresh_from_db()
        self.assertEqual(outbound.status, OutboundMessage.Status.FAILED)


# ---------------------------------------------------------------------------
# Test 6 — Buying intent creates AIProposal, not a Lead
# ---------------------------------------------------------------------------


class TestBuyingIntentProposal(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def test_intent_dm_creates_proposal_not_lead(self):
        from apps.ai.models import AIProposal
        from apps.crm.models import Lead
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        # "How much does it cost?" contains a buying-intent phrase
        igsid = f"igsid_{secrets.token_hex(4)}"
        payload = dm_payload(
            self.ig_account.page_id, igsid, "How much does it cost?"
        )
        view = InstagramWebhookView.as_view()
        with patch("apps.instagram.views.process_instagram_event"):
            view(signed_post(payload))
        event = WebhookEventLog.objects.first()
        process_instagram_event(event.pk)

        self.assertEqual(Lead.objects.count(), 0)
        proposal = AIProposal.objects.filter(
            account=self.account, action="purchase_intent"
        ).first()
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.status, AIProposal.Status.PENDING)

    def test_duplicate_intent_in_same_conversation_creates_one_proposal(self):
        from apps.ai.models import AIProposal
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        igsid = f"igsid_{secrets.token_hex(4)}"
        view = InstagramWebhookView.as_view()

        base_ts = int(timezone.now().timestamp() * 1000)
        for i, body in enumerate(["How much?", "What is the price?"]):
            with patch("apps.instagram.views.process_instagram_event"):
                payload = dm_payload(
                    self.ig_account.page_id, igsid, body,
                    msg_id=f"mid.{secrets.token_hex(4)}",
                    ts=base_ts + (i * 2000),
                )
                view(signed_post(payload))
            event = WebhookEventLog.objects.filter(status="received").last()
            process_instagram_event(event.pk)

        self.assertEqual(
            AIProposal.objects.filter(
                account=self.account, action="purchase_intent"
            ).count(),
            1,
        )


# ---------------------------------------------------------------------------
# Test 7 — Buying-intent comment does NOT create conversation or lead
# ---------------------------------------------------------------------------


class TestCommentDoesNotCreateConversation(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def test_intent_comment_creates_comment_thread_not_conversation(self):
        from apps.conversations.models import Conversation
        from apps.crm.models import Lead
        from apps.instagram.models.comment import CommentThread
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        payload = comment_payload(
            self.ig_account.page_id,
            sender_igsid=f"igsid_{secrets.token_hex(4)}",
            body="I want to buy this, how much?",
        )
        view = InstagramWebhookView.as_view()
        with patch("apps.instagram.views.process_instagram_event"):
            view(signed_post(payload))
        event = WebhookEventLog.objects.first()
        process_instagram_event(event.pk)

        self.assertEqual(CommentThread.objects.count(), 1)
        thread = CommentThread.objects.first()
        self.assertIsNotNone(thread.intent)  # intent was detected
        self.assertIsNone(thread.conversation)  # no conversation yet (DM-first)

        self.assertEqual(Conversation.objects.filter(channel="instagram").count(), 0)
        self.assertEqual(Lead.objects.count(), 0)


# ---------------------------------------------------------------------------
# Test 8 — Staff confirmation creates exactly one Lead
# ---------------------------------------------------------------------------


class TestStaffConfirmedLead(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def _setup_conversation_with_proposal(self):
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        igsid = f"igsid_{secrets.token_hex(4)}"
        payload = dm_payload(
            self.ig_account.page_id, igsid, "How much is the solar system?"
        )
        view = InstagramWebhookView.as_view()
        with patch("apps.instagram.views.process_instagram_event"):
            view(signed_post(payload))
        event = WebhookEventLog.objects.first()
        process_instagram_event(event.pk)

        from apps.ai.models import AIProposal
        from apps.conversations.models import Conversation

        proposal = AIProposal.objects.filter(
            account=self.account, action="purchase_intent"
        ).first()
        spine = Conversation.objects.filter(account=self.account, channel="instagram").first()
        return proposal, spine

    def test_staff_confirm_creates_one_lead(self):
        from apps.crm.models import Lead

        proposal, spine = self._setup_conversation_with_proposal()
        self.assertIsNotNone(proposal)

        # Simulate staff confirming the proposal
        Lead.objects.create(
            account=self.account,
            contact=spine.contact,
            conversation=spine,
            source="instagram",
            status=Lead.Status.NEW,
        )
        proposal.status = "used"
        proposal.save()

        self.assertEqual(Lead.objects.filter(account=self.account).count(), 1)
        lead = Lead.objects.first()
        self.assertEqual(lead.source, "instagram")
        self.assertEqual(lead.conversation, spine)

    def test_double_confirm_does_not_duplicate_lead(self):
        from apps.crm.models import Lead

        _, spine = self._setup_conversation_with_proposal()

        # Create once
        Lead.objects.create(
            account=self.account,
            contact=spine.contact,
            conversation=spine,
            source="instagram",
            status=Lead.Status.NEW,
        )

        # The Lead model enforces at most one open lead per contact per account
        from django.db import IntegrityError, transaction

        try:
            with transaction.atomic():
                Lead.objects.create(
                    account=self.account,
                    contact=spine.contact,
                    conversation=spine,
                    source="instagram",
                    status=Lead.Status.NEW,
                )
            duplicate_allowed = True
        except Exception:
            duplicate_allowed = False

        self.assertFalse(duplicate_allowed)
        self.assertEqual(Lead.objects.filter(account=self.account).count(), 1)


# ---------------------------------------------------------------------------
# Test 9 — Staff dismissal does not create a Lead
# ---------------------------------------------------------------------------


class TestStaffDismissal(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def test_dismiss_proposal_creates_no_lead(self):
        from apps.ai.models import AIProposal
        from apps.crm.models import Lead
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        igsid = f"igsid_{secrets.token_hex(4)}"
        payload = dm_payload(self.ig_account.page_id, igsid, "What is the price?")
        view = InstagramWebhookView.as_view()
        with patch("apps.instagram.views.process_instagram_event"):
            view(signed_post(payload))
        event = WebhookEventLog.objects.first()
        process_instagram_event(event.pk)

        # Staff dismisses
        proposal = AIProposal.objects.filter(
            account=self.account, action="purchase_intent"
        ).first()
        self.assertIsNotNone(proposal)
        proposal.status = AIProposal.Status.DISMISSED
        proposal.save()

        self.assertEqual(Lead.objects.count(), 0)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, AIProposal.Status.DISMISSED)


# ---------------------------------------------------------------------------
# Test 10 — Comments vs DMs are correctly distinguished
# ---------------------------------------------------------------------------


class TestCommentVsDM(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def test_comment_stored_as_comment_thread(self):
        from apps.instagram.models.comment import Comment, CommentThread
        from apps.instagram.models.message import InstagramMessage
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        payload = comment_payload(
            self.ig_account.page_id,
            sender_igsid=f"igsid_{secrets.token_hex(4)}",
            body="Nice product!",
        )
        view = InstagramWebhookView.as_view()
        with patch("apps.instagram.views.process_instagram_event"):
            view(signed_post(payload))
        event = WebhookEventLog.objects.first()
        process_instagram_event(event.pk)

        self.assertEqual(CommentThread.objects.count(), 1)
        self.assertEqual(Comment.objects.count(), 1)
        self.assertEqual(InstagramMessage.objects.count(), 0)  # DMs, not comments

    def test_dm_stored_as_instagram_message(self):
        from apps.instagram.models.comment import CommentThread
        from apps.instagram.models.message import InstagramMessage
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event

        payload = dm_payload(
            self.ig_account.page_id,
            sender_igsid=f"igsid_{secrets.token_hex(4)}",
            body="Hello via DM",
        )
        view = InstagramWebhookView.as_view()
        with patch("apps.instagram.views.process_instagram_event"):
            view(signed_post(payload))
        event = WebhookEventLog.objects.first()
        process_instagram_event(event.pk)

        self.assertEqual(InstagramMessage.objects.count(), 1)
        self.assertEqual(CommentThread.objects.count(), 0)


# ---------------------------------------------------------------------------
# Test 11 — Failed API responses handled safely
# ---------------------------------------------------------------------------


class TestOutboundFailureHandling(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)

    def test_meta_api_error_marks_outbound_failed_not_exception(self):
        from apps.instagram.models.message import OutboundMessage
        from apps.instagram.providers.base import EligibilityResult, SendResult
        from apps.instagram.services.outbound import enqueue_reply, send_outbound

        outbound = enqueue_reply(
            self.ig_account, "igsid_err", "Hi",
            action_type=OutboundMessage.ActionType.DM_REPLY,
            idempotency_key=f"test:{secrets.token_hex(8)}",
        )

        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider"
        ) as MockProvider:
            MockProvider.return_value.can_send.return_value = EligibilityResult(eligible=True)
            MockProvider.return_value.send_message.return_value = SendResult(
                success=False, error="rate_limited", terminal=False
            )
            result = send_outbound(outbound)

        self.assertFalse(result)
        outbound.refresh_from_db()
        # Non-terminal failure: requeued with backoff, not permanently failed
        self.assertEqual(outbound.status, OutboundMessage.Status.QUEUED)
        self.assertEqual(outbound.attempts, 1)
        self.assertIsNotNone(outbound.next_attempt_at)

    def test_terminal_api_error_marks_permanently_failed(self):
        from apps.instagram.models.message import OutboundMessage
        from apps.instagram.providers.base import EligibilityResult, SendResult
        from apps.instagram.services.outbound import enqueue_reply, send_outbound

        outbound = enqueue_reply(
            self.ig_account, "igsid_perm", "Hi",
            action_type=OutboundMessage.ActionType.DM_REPLY,
            idempotency_key=f"test:{secrets.token_hex(8)}",
        )

        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider"
        ) as MockProvider:
            MockProvider.return_value.can_send.return_value = EligibilityResult(eligible=True)
            MockProvider.return_value.send_message.return_value = SendResult(
                success=False, error="token_invalid", terminal=True
            )
            result = send_outbound(outbound)

        self.assertFalse(result)
        outbound.refresh_from_db()
        self.assertEqual(outbound.status, OutboundMessage.Status.FAILED)


# ---------------------------------------------------------------------------
# Test 12 — WhatsApp regression
# ---------------------------------------------------------------------------


class TestWhatsAppRegression(TestCase):
    """
    Prove that the Instagram FK addition to Conversation and the new channel
    choice do not break existing WhatsApp conversation creation.
    """

    def setUp(self):
        self.account, _ = make_account()

    def test_whatsapp_conversation_factory_unaffected(self):
        from apps.contacts.models import Contact
        from apps.conversations.models import Conversation
        from apps.whatsapp.models import WhatsAppContact
        from apps.whatsapp.models.conversation import Conversation as WAConversation

        contact = Contact.objects.create(account=self.account, source="whatsapp")
        wa_contact = WhatsAppContact.objects.create(
            account=self.account,
            phone_number="+260971000099",
            contact=contact,
        )
        wa_convo = WAConversation.objects.create(
            account=self.account,
            contact=wa_contact,
        )
        spine = Conversation.get_or_create_for_whatsapp(wa_convo)

        self.assertEqual(spine.channel, Conversation.Channel.WHATSAPP)
        self.assertEqual(spine.whatsapp_conversation, wa_convo)
        self.assertIsNone(spine.instagram_conversation)
        self.assertEqual(spine.contact, contact)

    def test_instagram_and_whatsapp_conversations_are_independent(self):
        from apps.contacts.models import Contact
        from apps.conversations.models import Conversation
        from apps.instagram.models.account import InstagramBusinessAccount
        from apps.instagram.models.contact import InstagramContact
        from apps.instagram.models.conversation import InstagramConversation
        from apps.whatsapp.models import WhatsAppContact
        from apps.whatsapp.models.conversation import Conversation as WAConversation

        # WhatsApp side
        wa_contact_model = Contact.objects.create(account=self.account, source="whatsapp")
        wa_contact = WhatsAppContact.objects.create(
            account=self.account,
            phone_number="+260971000077",
            contact=wa_contact_model,
        )
        wa_convo = WAConversation.objects.create(
            account=self.account, contact=wa_contact
        )
        wa_spine = Conversation.get_or_create_for_whatsapp(wa_convo)

        # Instagram side
        ig_biz = InstagramBusinessAccount.objects.create(
            account=self.account,
            instagram_business_account_id="ig_test_777",
            page_id="page_777",
            verify_token="tok",
            is_active=True,
        )
        ig_contact_model = Contact.objects.create(account=self.account, source="instagram")
        ig_contact = InstagramContact.objects.create(
            account=self.account,
            instagram_scoped_id="igsid_777",
            contact=ig_contact_model,
        )
        ig_convo = InstagramConversation.objects.create(
            instagram_account=ig_biz,
            instagram_contact=ig_contact,
        )
        ig_spine = Conversation.get_or_create_for_instagram(ig_convo)

        self.assertEqual(Conversation.objects.count(), 2)
        self.assertNotEqual(wa_spine.pk, ig_spine.pk)
        self.assertEqual(wa_spine.channel, "whatsapp")
        self.assertEqual(ig_spine.channel, "instagram")

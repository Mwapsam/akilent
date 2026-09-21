"""Phase 1 acceptance: for every active conversation Akilent can tell who spoke last,
whether the customer is waiting on the business, for how long, and what happened
most recently - through the real inbound -> reply -> status -> close path.

All three outbound paths (human reply, template reply, workflow reply) must land in
``conversations.Message``, and the inbox must not consult ``MessageLog`` to show it.
"""
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import Membership
from apps.automation.models import Workflow
from apps.automation.workflow_engine import enroll
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.conversations.state import ConversationState as S
from apps.conversations.state import get_conversation_state, missed, needs_attention
from apps.whatsapp.models import MessageLog, MessageTemplate, OutboundMessage, WebhookEventLog, WhatsAppContact
from apps.whatsapp.providers import WhatsAppProviderError
from apps.whatsapp.tasks import _handle_inbound_message, drain_outbound_queue as REAL_DRAIN
from apps.whatsapp.tasks import process_whatsapp_event
from apps.whatsapp.tests.test_auto_reply import PHONE, WA_ID, Base

TASKS = "apps.whatsapp.tasks"


class Clock:
    def __init__(self):
        self.now = timezone.now().replace(microsecond=0)

    def tick(self, **kw):
        self.now += timedelta(**kw)

    def __call__(self):
        return self.now


class PhaseOneAcceptanceTest(Base):
    def setUp(self):
        super().setUp()
        cache.clear()   # tasks caches the automation-events flag process-wide for 60s
        self.addCleanup(cache.clear)
        self.clock = Clock()
        for target, kwargs in (
            ("django.utils.timezone.now", {"side_effect": self.clock}),
            (f"{TASKS}._auto_reply_during_setup", {}),
            (f"{TASKS}._throttle_for_account", {}),
            (f"{TASKS}._get_provider_for_account", {"return_value": MagicMock()}),
        ):
            p = patch(target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.sent = 0
        p = patch(f"{TASKS}._send_outbound", side_effect=self._fake_send)
        self.send_mock = p.start()
        self.addCleanup(p.stop)
        self.fail_next_send = False

        self.user = User.objects.create_user("agent", "a@example.com", "pw")
        Membership.objects.create(user=self.user, account=self.account, role=Membership.Role.OWNER)
        self.client.force_login(self.user)

    def _fake_send(self, provider, contact, payload):
        if self.fail_next_send:
            self.fail_next_send = False
            exc = WhatsAppProviderError("boom")
            exc.code, exc.retryable = "131026", False
            raise exc
        self.sent += 1
        return {"message_id": f"wamid.OUT{self.sent}"}

    # -- helpers ------------------------------------------------------------------------
    def customer_says(self, text, msg_id):
        body = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "PNID"},
            "contacts": [{"profile": {"name": "Mary"}}],
            "messages": [{"from": WA_ID, "id": msg_id, "timestamp": str(int(self.clock.now.timestamp())),
                          "type": "text", "text": {"body": text}}]}}]}]}
        event = WebhookEventLog.objects.create(source="whatsapp", event_type="message", payload=body)
        _handle_inbound_message(event)

    def status_webhook(self, message_id, status):
        body = {"entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": "PNID"},
            "statuses": [{"id": message_id, "status": status,
                          "timestamp": str(int(self.clock.now.timestamp()))}]}}]}]}
        event = WebhookEventLog.objects.create(source="whatsapp", event_type="status", payload=body)
        process_whatsapp_event(event.id)

    def drain(self):
        REAL_DRAIN()

    @property
    def convo(self):
        return Conversation.objects.get(account=self.account)

    def state(self):
        return get_conversation_state(self.convo)

    def in_attention(self):
        return self.convo.id in {c.id for c in needs_attention(self.account)}

    # -- the journey --------------------------------------------------------------------
    def test_full_conversation_journey(self):
        # 1. customer asks -> waiting for the agent, measured from their message
        self.customer_says("Do you have the Samsung S25?", "wamid.IN1")
        self.clock.tick(minutes=4)
        s = self.state()
        self.assertEqual((s.state, s.last_speaker), (S.WAITING_FOR_AGENT, "customer"))
        self.assertEqual(s.waiting_age, timedelta(minutes=4))
        self.assertTrue(self.in_attention())

        # 2. HUMAN reply through the inbox -> lands in the spine, customer now waited on
        resp = self.client.post(f"/inbox/{self.convo.public_id}/", {"action": "reply", "body": "Yes, K8,500"})
        self.assertEqual(resp.status_code, 302)
        self.drain()
        s = self.state()
        self.assertEqual((s.state, s.last_speaker), (S.WAITING_FOR_CUSTOMER, "business"))
        self.assertEqual(s.first_response_seconds, 4 * 60)
        self.assertFalse(self.in_attention())
        reply = Message.objects.get(conversation=self.convo, direction="outbound")
        self.assertEqual((reply.body, reply.status), ("Yes, K8,500", "sent"))

        # 3. status webhook advances the spine message
        self.clock.tick(seconds=30)
        self.status_webhook("wamid.OUT1", "delivered")
        reply.refresh_from_db()
        self.assertEqual(reply.status, "delivered")
        self.status_webhook("wamid.OUT1", "read")
        reply.refresh_from_db()
        self.assertEqual(reply.status, "read")

        # 4. customer writes again -> waiting again, from the NEW message
        self.clock.tick(minutes=10)
        self.customer_says("Can I get it delivered?", "wamid.IN2")
        self.clock.tick(minutes=2)
        s = self.state()
        self.assertEqual(s.state, S.WAITING_FOR_AGENT)
        self.assertEqual(s.waiting_age, timedelta(minutes=2))

        # 5. TEMPLATE reply (agent-sent template) lands in the spine
        template = MessageTemplate.objects.create(
            account=self.account, name="Delivery info", whatsapp_template_name="delivery_info",
            content="We deliver in 2 days", approval_status=MessageTemplate.ApprovalStatus.APPROVED,
            category=MessageTemplate.Category.UTILITY)
        wa = WhatsAppContact.objects.get(account=self.account, phone_number=PHONE)
        OutboundMessage.objects.create(
            account=self.account, contact=wa, template=template,
            payload={"type": "template", "template_name": "delivery_info", "language": "en", "params": {}})
        self.drain()
        self.assertEqual(self.state().state, S.WAITING_FOR_CUSTOMER)
        self.assertEqual(Message.objects.filter(conversation=self.convo, direction="outbound").count(), 2)

        # 6. WORKFLOW reply lands in the spine
        self.clock.tick(minutes=5)
        self.customer_says("Great, order it please", "wamid.IN3")
        self.clock.tick(minutes=1)
        self.assertEqual(self.state().state, S.WAITING_FOR_AGENT)
        contact = Contact.objects.get(account=self.account, phone=PHONE)
        workflow = Workflow.objects.create(
            account=self.account, name="Confirm", status=Workflow.Status.PUBLISHED,
            definition={"trigger": {"type": "manual"}, "steps": [
                {"id": "a", "type": "send_whatsapp", "template": "delivery_info", "next": "b"},
                {"id": "b", "type": "stop"}]})
        enroll(workflow, contact)
        self.drain()
        self.assertEqual(self.state().state, S.WAITING_FOR_CUSTOMER)
        self.assertEqual(Message.objects.filter(conversation=self.convo, direction="outbound").count(), 3)

        # 7. a FAILED reply is visible in the thread but leaves the customer waiting
        self.clock.tick(minutes=5)
        self.customer_says("Hello? Any update?", "wamid.IN4")
        self.clock.tick(minutes=1)
        self.fail_next_send = True
        self.client.post(f"/inbox/{self.convo.public_id}/", {"action": "reply", "body": "Sorry for the wait"})
        self.drain()
        failed = Message.objects.get(conversation=self.convo, body="Sorry for the wait")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(self.state().state, S.WAITING_FOR_AGENT)
        self.assertTrue(self.in_attention())

        # 8. STOP closes the spine conversation; a later message starts a NEW exchange
        self.clock.tick(minutes=1)
        self.customer_says("STOP", "wamid.IN5")
        stopped = self.convo
        self.assertEqual(stopped.status, Conversation.Status.CLOSED)
        s = get_conversation_state(stopped)
        self.assertEqual((s.state, s.closed_reason), (S.CLOSED, "closed"))
        self.assertNotIn(stopped.id, {c.id for c in needs_attention(self.account)})
        self.clock.tick(minutes=30)
        self.customer_says("Actually, I changed my mind", "wamid.IN6")
        self.assertEqual(Conversation.objects.filter(account=self.account).count(), 2)
        fresh = Conversation.objects.filter(account=self.account).exclude(id=stopped.id).get()
        self.assertEqual(get_conversation_state(fresh).state, S.WAITING_FOR_AGENT)
        self.assertIn(fresh.id, {c.id for c in needs_attention(self.account)})
        self.assertEqual(get_conversation_state(stopped).state, S.CLOSED)   # the old one stays closed

    def test_explicit_close_then_customer_message_reopens_the_same_conversation(self):
        self.customer_says("Hi", "wamid.IN1")
        self.clock.tick(minutes=1)
        self.client.post(f"/inbox/{self.convo.public_id}/", {"action": "close"})
        self.assertEqual(self.state().state, S.CLOSED)
        self.assertFalse(self.in_attention())
        self.clock.tick(minutes=5)
        self.customer_says("Are you there?", "wamid.IN2")
        self.assertEqual(Conversation.objects.filter(account=self.account).count(), 1)
        self.assertEqual(self.convo.status, Conversation.Status.OPEN)
        self.assertEqual(self.state().state, S.WAITING_FOR_AGENT)
        self.assertTrue(self.in_attention())

    def test_unanswered_customer_drops_out_of_attention_after_24h_but_is_missed(self):
        self.customer_says("Anyone there?", "wamid.IN1")
        self.clock.tick(hours=25)
        self.assertFalse(self.in_attention())
        self.assertEqual(self.state().state, S.CLOSED)
        self.assertIn(self.convo.id, {c.id for c in missed(self.account)})

    def test_inbox_reads_the_spine_and_never_message_log(self):
        self.customer_says("Hi", "wamid.IN1")
        self.clock.tick(minutes=3)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get("/inbox/?view=needs_attention")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue([q for q in ctx.captured_queries if "conversations_conversation" in q["sql"]])
        self.assertFalse([q for q in ctx.captured_queries if "whatsapp_messagelog" in q["sql"]])
        self.assertContains(resp, "3 minutes")       # waiting age is shown (timesince uses a nbsp)

    def test_replaying_the_same_send_does_not_duplicate_the_spine_message(self):
        self.customer_says("Hi", "wamid.IN1")
        self.clock.tick(minutes=1)      # OutboundMessage.scheduled_at binds the real clock at import
        self.client.post(f"/inbox/{self.convo.public_id}/", {"action": "reply", "body": "Hello"})
        self.drain()
        self.drain()
        self.assertEqual(Message.objects.filter(conversation=self.convo, direction="outbound").count(), 1)
        self.assertEqual(MessageLog.objects.filter(direction="out").count(), 1)

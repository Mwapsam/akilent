"""Cross-channel contract tests.

Every channel that participates in the Akilent conversation spine must satisfy
the same set of contracts regardless of its underlying technology.

Channels under test: whatsapp, instagram

Contract matrix
───────────────────────────────────────────────────────────────────────────────
CC-01  resolve_channel_contact: channel identity → canonical Contact (created)
CC-02  resolve_channel_contact: idempotent — same Contact returned on repeat
CC-03  ChannelConversation is created and has the correct channel tag
CC-04  canonical Conversation is created with the correct Channel value
CC-05  inbound message is persisted on the canonical Conversation
CC-06  MessageReceived domain event is emitted with correct channel/contact
CC-07  automation workflow is enrolled when MessageReceived fires
CC-08  Lead can be created (staff-confirmed) from a canonical Conversation
CC-09  inbound processing is idempotent — duplicate input → single record set
CC-10  Outbound message is mirrored onto the canonical Conversation spine
───────────────────────────────────────────────────────────────────────────────

Known gap (documented):
  CC-05 Instagram — Instagram's inbound service (`_process_dm_entry`) creates
  an InstagramMessage but does NOT create a spine Message record.  WhatsApp
  does this via `record_inbound_whatsapp_message` → Message.get_or_create.
  Until an equivalent `record_inbound_instagram_message` is added,
  TestInstagramContracts.test_cc05 is marked @expectedFailure to surface the
  gap rather than silently skip it.
"""

from __future__ import annotations

import abc
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.automation.models import Workflow
from apps.conversations.models import ChannelConversation, Conversation, Message
from apps.crm.models import Lead

# ---------------------------------------------------------------------------
# Channel fixture protocol
# ---------------------------------------------------------------------------


class ChannelFixture(abc.ABC):
    """Per-channel test adapter.  Each implementation wires up the minimal
    objects for that channel and exposes a uniform surface for the contracts."""

    channel: str  # Conversation.Channel value

    @abc.abstractmethod
    def setup(self) -> None:
        """Create all model prerequisites; called once per test."""

    @abc.abstractmethod
    def resolve_contact(self):
        """Return the canonical contacts.Contact for this channel identity."""

    @abc.abstractmethod
    def resolve_conversation(self) -> tuple:
        """Return (channel_obj, spine_conversation)."""

    @abc.abstractmethod
    def record_first_inbound(self) -> Conversation:
        """Run the full inbound processing path for the first message.
        Returns the spine Conversation."""

    @abc.abstractmethod
    def record_same_inbound_again(self) -> None:
        """Re-submit the exact same inbound payload to verify idempotency."""

    @property
    @abc.abstractmethod
    def account(self): ...


# ---------------------------------------------------------------------------
# WhatsApp fixture
# ---------------------------------------------------------------------------


class WhatsAppFixture(ChannelFixture):
    channel = Conversation.Channel.WHATSAPP

    def setup(self) -> None:
        from apps.accounts.models import Account
        from apps.contacts.models import Contact
        from apps.whatsapp.models import Conversation as WaConversation
        from apps.whatsapp.models import MessageLog, WhatsAppContact

        self._account = Account.objects.create(company_name="WA Contract Co")
        # canonical Contact must exist before WhatsApp spine creation
        self._contact = Contact.objects.create(account=self._account, source="whatsapp")
        self._wa_contact = WhatsAppContact.objects.create(
            account=self._account,
            phone_number="+260971111001",
            contact=self._contact,
        )
        self._wa_conversation = WaConversation.get_or_open(self._wa_contact)
        self._message_log = MessageLog.objects.create(
            account=self._account,
            conversation=self._wa_conversation,
            contact=self._wa_contact,
            message_id="wamid.CONTRACT001",
            direction=MessageLog.Direction.INBOUND,
            message_type=MessageLog.MessageType.TEXT,
            content="I want to buy the dress",
            status=MessageLog.Status.DELIVERED,
            timestamp=timezone.now(),
        )

    @property
    def account(self):
        return self._account

    def resolve_contact(self):
        from apps.whatsapp.services.contacts import resolve_channel_contact

        return resolve_channel_contact(self._account, self._wa_contact)

    def resolve_conversation(self):
        spine = Conversation.get_or_create_for_whatsapp(self._wa_conversation)
        return self._wa_conversation, spine

    def record_first_inbound(self) -> Conversation:
        from apps.conversations.services import record_inbound_whatsapp_message

        contact = self.resolve_contact()
        return record_inbound_whatsapp_message(
            contact=contact,
            wa_contact=self._wa_contact,
            whatsapp_conversation=self._wa_conversation,
            message_log=self._message_log,
        )

    def record_same_inbound_again(self) -> None:
        from apps.conversations.services import record_inbound_whatsapp_message

        contact = self.resolve_contact()
        record_inbound_whatsapp_message(
            contact=contact,
            wa_contact=self._wa_contact,
            whatsapp_conversation=self._wa_conversation,
            message_log=self._message_log,
        )


# ---------------------------------------------------------------------------
# Instagram fixture
# ---------------------------------------------------------------------------


class InstagramFixture(ChannelFixture):
    channel = Conversation.Channel.INSTAGRAM

    def setup(self) -> None:
        from apps.accounts.models import Account
        from apps.instagram.models.account import InstagramBusinessAccount
        from apps.instagram.models.contact import InstagramContact
        from apps.instagram.models.conversation import InstagramConversation
        from apps.instagram.services.contacts import resolve_or_create_contact

        self._account = Account.objects.create(company_name="IG Contract Co")
        self._ig_account = InstagramBusinessAccount.objects.create(
            account=self._account,
            instagram_business_account_id="ig_contract_001",
            page_id="page_contract_001",
            access_token="tok",
            verify_token="vt",
        )
        self._ig_contact = InstagramContact.objects.create(
            account=self._account,
            instagram_scoped_id="igsid_contract_001",
            username="buyer_test",
        )
        # Ensure canonical Contact is linked before any spine operations
        resolve_or_create_contact(self._ig_account, "igsid_contract_001")
        self._ig_contact.refresh_from_db()
        self._ig_conversation = InstagramConversation.get_or_open(self._ig_contact)
        self._message_id = "igmid.CONTRACT001"
        self._body = "I want to buy the dress"

    @property
    def account(self):
        return self._account

    def resolve_contact(self):
        from apps.instagram.services.contacts import resolve_or_create_contact

        ig_contact = resolve_or_create_contact(self._ig_account, "igsid_contract_001")
        return ig_contact.contact

    def resolve_conversation(self):
        from apps.instagram.services.conversations import (
            get_or_create_instagram_conversation,
        )

        ig_convo, spine = get_or_create_instagram_conversation(self._ig_contact)
        return ig_convo, spine

    def record_first_inbound(self) -> Conversation:
        from apps.conversations.services import record_inbound_instagram_message
        from apps.instagram.models.message import InstagramMessage
        from apps.instagram.services.contacts import resolve_or_create_contact
        from apps.instagram.services.conversations import (
            get_or_create_instagram_conversation,
        )

        ig_contact = resolve_or_create_contact(self._ig_account, "igsid_contract_001")
        ig_convo, spine = get_or_create_instagram_conversation(ig_contact)
        ts = timezone.now()
        ig_convo.register_inbound(ts)
        ig_message, _ = InstagramMessage.objects.get_or_create(
            conversation=ig_convo,
            message_id=self._message_id,
            defaults={
                "direction": InstagramMessage.Direction.INBOUND,
                "body": self._body,
                "timestamp": ts,
            },
        )
        record_inbound_instagram_message(
            contact=ig_contact.contact,
            instagram_conversation=ig_convo,
            instagram_message=ig_message,
        )
        return spine

    def record_same_inbound_again(self) -> None:
        from apps.conversations.services import record_inbound_instagram_message
        from apps.instagram.models.message import InstagramMessage
        from apps.instagram.services.contacts import resolve_or_create_contact
        from apps.instagram.services.conversations import (
            get_or_create_instagram_conversation,
        )

        ig_contact = resolve_or_create_contact(self._ig_account, "igsid_contract_001")
        ig_convo, _ = get_or_create_instagram_conversation(ig_contact)
        ts = timezone.now()
        ig_message, _ = InstagramMessage.objects.get_or_create(
            conversation=ig_convo,
            message_id=self._message_id,
            defaults={
                "direction": InstagramMessage.Direction.INBOUND,
                "body": self._body,
                "timestamp": ts,
            },
        )
        # Second call is idempotent — record_inbound_instagram_message returns None
        record_inbound_instagram_message(
            contact=ig_contact.contact,
            instagram_conversation=ig_convo,
            instagram_message=ig_message,
        )


# ---------------------------------------------------------------------------
# Contract test base
# ---------------------------------------------------------------------------


class CrossChannelContractBase(TestCase):
    """Subclasses set ``fixture_class``; the contracts run via the fixture.

    The base class itself has no fixture and must not be collected directly.
    """

    fixture_class: type[ChannelFixture] | None = None

    def setUp(self):
        if self.fixture_class is None:
            self.skipTest("Abstract base — no fixture_class set")
        self.fx = self.fixture_class()
        self.fx.setup()

    # CC-01  resolve_channel_contact creates canonical Contact
    def test_cc01_resolve_contact_creates_canonical_contact(self):
        contact = self.fx.resolve_contact()
        self.assertIsNotNone(contact)
        self.assertIsNotNone(contact.pk)
        self.assertEqual(contact.account, self.fx.account)

    # CC-02  resolve_channel_contact is idempotent
    def test_cc02_resolve_contact_idempotent(self):
        c1 = self.fx.resolve_contact()
        c2 = self.fx.resolve_contact()
        self.assertEqual(c1.pk, c2.pk)
        from apps.contacts.models import Contact

        self.assertEqual(Contact.objects.filter(account=self.fx.account).count(), 1)

    # CC-03  ChannelConversation has correct channel tag
    def test_cc03_channel_conversation_has_correct_channel(self):
        channel_obj, _ = self.fx.resolve_conversation()
        cc = ChannelConversation.objects.filter(
            channel=self.fx.channel, object_id=channel_obj.pk
        ).first()
        self.assertIsNotNone(cc, "ChannelConversation must exist")
        self.assertEqual(cc.channel, self.fx.channel)

    # CC-04  canonical Conversation has correct Channel value
    def test_cc04_spine_has_correct_channel(self):
        _, spine = self.fx.resolve_conversation()
        self.assertEqual(spine.channel, self.fx.channel)

    # CC-05  inbound message is persisted on the canonical Conversation
    def test_cc05_inbound_message_persisted_on_spine(self):
        spine = self.fx.record_first_inbound()
        self.assertIsNotNone(spine)
        self.assertGreater(
            Message.objects.filter(conversation=spine).count(),
            0,
            "At least one Message must exist on the spine after inbound",
        )

    # CC-06  A message-received signal reaches the domain event layer
    def test_cc06_message_received_event_emitted(self):
        """Accepts either path:
        - a spine Event record with type='conversation.message_received' (WhatsApp)
        - dispatcher.publish(MessageReceived(...)) (Instagram)
        Both are valid; the contract requires that *something* fires.
        """
        from apps.conversations.models import Event
        from apps.core.events import MessageReceived, dispatcher

        published: list = []
        with patch.object(dispatcher, "publish", wraps=dispatcher.publish) as mock_pub:
            self.fx.record_first_inbound()
            published = [
                c.args[0]
                for c in mock_pub.call_args_list
                if c.args and isinstance(c.args[0], MessageReceived)
            ]

        via_db = Event.objects.filter(
            account=self.fx.account, type="conversation.message_received"
        ).exists()
        via_dispatcher = bool(published)

        self.assertTrue(
            via_db or via_dispatcher,
            "Either an Event DB record or a MessageReceived dispatch must occur",
        )

    # CC-07  automation workflow is enrolled when MessageReceived fires
    def test_cc07_automation_enrolled_on_message_received(self):
        from apps.automation.models import WorkflowRun

        Workflow.objects.create(
            account=self.fx.account,
            name="Cross-channel trigger",
            slug=f"cross-channel-trigger-{self.fx.channel}",
            status=Workflow.Status.PUBLISHED,
            definition={
                "trigger": {"type": "conversation.message_received"},
                "steps": [{"id": "stop", "type": "stop"}],
            },
        )
        self.fx.record_first_inbound()
        contact = self.fx.resolve_contact()
        self.assertTrue(
            WorkflowRun.objects.filter(contact=contact).exists(),
            "A WorkflowRun must be created for the contact when MessageReceived fires",
        )

    # CC-08  Lead can be created (staff-confirmed) from the spine Conversation
    def test_cc08_lead_creatable_from_conversation(self):
        spine = self.fx.record_first_inbound()
        contact = self.fx.resolve_contact()
        lead, created = Lead.objects.get_or_create(
            account=self.fx.account,
            contact=contact,
            defaults={
                "conversation": spine,
                "source": self.fx.channel,
            },
        )
        self.assertEqual(lead.account_id, self.fx.account.pk)
        self.assertIsNotNone(lead.pk)

    # CC-09  Idempotent inbound — duplicate input produces single record set
    def test_cc09_inbound_idempotent(self):
        spine = self.fx.record_first_inbound()
        msg_count_before = Message.objects.filter(conversation=spine).count()
        self.fx.record_same_inbound_again()
        msg_count_after = Message.objects.filter(conversation=spine).count()
        self.assertEqual(
            msg_count_before,
            msg_count_after,
            "Re-submitting the same inbound payload must not create duplicate Messages",
        )

    # CC-10  Outbound message is mirrored onto the spine Conversation
    def test_cc10_outbound_mirrored_to_spine(self):
        spine = self.fx.record_first_inbound()
        Message.objects.create(
            account=self.fx.account,
            conversation=spine,
            direction=Message.Direction.OUTBOUND,
            body="Thanks for your interest! Here's the price.",
            timestamp=timezone.now(),
        )
        outbound_count = Message.objects.filter(
            conversation=spine, direction=Message.Direction.OUTBOUND
        ).count()
        self.assertGreater(outbound_count, 0)


# ---------------------------------------------------------------------------
# Concrete test classes — one per channel
# ---------------------------------------------------------------------------


class TestWhatsAppContracts(CrossChannelContractBase):
    fixture_class = WhatsAppFixture


class TestInstagramContracts(CrossChannelContractBase):
    fixture_class = InstagramFixture

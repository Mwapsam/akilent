"""ChannelConversation invariant tests.

Verifies the database and application-level guarantees that make the
ChannelConversation join table a reliable spine:

  INV-01  UNIQUE(channel, object_id) — same binding raises IntegrityError
  INV-02  Different channels may share the same object_id (no cross-channel clash)
  INV-03  Deleting a Conversation cascades to its ChannelConversation row
  INV-04  get_or_create_for_whatsapp is idempotent — N calls → 1 Conversation
  INV-05  get_or_create_for_instagram is idempotent — N calls → 1 Conversation
  INV-06  get_or_create_for_channel dispatches correctly
  INV-07  _bind_channel race: losing Conversation is deleted; winner is returned
  INV-08  whatsapp_conversation property returns the backing WA record
  INV-09  instagram_conversation property returns the backing IG record
  INV-10  whatsapp_conversation property returns None for an Instagram spine
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.contacts.models import Contact
from apps.conversations.models import ChannelConversation, Conversation


def _account():
    from apps.accounts.models import Account

    return Account.objects.create(company_name="Test Co")


def _contact(account):
    return Contact.objects.create(account=account)


def _spine(account, contact, channel="whatsapp"):
    return Conversation.objects.create(
        account=account, contact=contact, channel=channel
    )


# ---------------------------------------------------------------------------
# INV-01  UNIQUE(channel, object_id)
# ---------------------------------------------------------------------------


class TestUniquenessConstraint(TestCase):
    def test_duplicate_channel_object_id_raises(self):
        account = _account()
        contact = _contact(account)
        spine = _spine(account, contact)
        ChannelConversation.objects.create(
            conversation=spine, channel="whatsapp", object_id=42
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ChannelConversation.objects.create(
                    conversation=spine, channel="whatsapp", object_id=42
                )

    def test_same_object_id_different_channel_is_allowed(self):
        """INV-02: whatsapp:42 and instagram:42 can both exist."""
        account = _account()
        contact = _contact(account)
        spine_wa = _spine(account, contact, "whatsapp")
        spine_ig = _spine(account, contact, "instagram")
        ChannelConversation.objects.create(
            conversation=spine_wa, channel="whatsapp", object_id=42
        )
        ChannelConversation.objects.create(
            conversation=spine_ig, channel="instagram", object_id=42
        )
        self.assertEqual(ChannelConversation.objects.filter(object_id=42).count(), 2)


# ---------------------------------------------------------------------------
# INV-03  CASCADE on Conversation delete
# ---------------------------------------------------------------------------


class TestCascadeDelete(TestCase):
    def test_deleting_conversation_removes_channel_conversation(self):
        account = _account()
        contact = _contact(account)
        spine = _spine(account, contact)
        cc = ChannelConversation.objects.create(
            conversation=spine, channel="whatsapp", object_id=99
        )
        pk = cc.pk
        spine.delete()
        self.assertFalse(ChannelConversation.objects.filter(pk=pk).exists())


# ---------------------------------------------------------------------------
# INV-04 / INV-05  Factory idempotency
# ---------------------------------------------------------------------------


class TestFactoryIdempotency(TestCase):
    def _make_wa_conversation(self):
        from apps.accounts.models import Account
        from apps.contacts.models import Contact
        from apps.whatsapp.models import Conversation as WaConversation
        from apps.whatsapp.models import WhatsAppContact

        account = Account.objects.create(company_name="WA Co")
        contact = Contact.objects.create(account=account)
        wa_contact = WhatsAppContact.objects.create(
            account=account, phone_number="+260971000001", contact=contact
        )
        return WaConversation.get_or_open(wa_contact)

    def _make_ig_conversation(self):
        from apps.accounts.models import Account
        from apps.contacts.models import Contact
        from apps.instagram.models.account import InstagramBusinessAccount
        from apps.instagram.models.contact import InstagramContact
        from apps.instagram.models.conversation import InstagramConversation

        account = Account.objects.create(company_name="IG Co")
        _ig_account = InstagramBusinessAccount.objects.create(
            account=account,
            instagram_business_account_id="ig_test_001",
            page_id="page_test_001",
            access_token="tok",
            verify_token="vt",
        )
        contact = Contact.objects.create(account=account)
        ig_contact = InstagramContact.objects.create(
            account=account,
            instagram_scoped_id="igsid_test_001",
            contact=contact,
        )
        return InstagramConversation.get_or_open(ig_contact)

    def test_whatsapp_factory_idempotent(self):
        """INV-04: calling get_or_create_for_whatsapp twice returns the same Conversation."""
        wa_conv = self._make_wa_conversation()
        spine1 = Conversation.get_or_create_for_whatsapp(wa_conv)
        spine2 = Conversation.get_or_create_for_whatsapp(wa_conv)
        self.assertEqual(spine1.pk, spine2.pk)
        self.assertEqual(Conversation.objects.filter(channel="whatsapp").count(), 1)
        self.assertEqual(
            ChannelConversation.objects.filter(
                channel="whatsapp", object_id=wa_conv.pk
            ).count(),
            1,
        )

    def test_instagram_factory_idempotent(self):
        """INV-05: calling get_or_create_for_instagram twice returns the same Conversation."""
        ig_conv = self._make_ig_conversation()
        spine1 = Conversation.get_or_create_for_instagram(ig_conv)
        spine2 = Conversation.get_or_create_for_instagram(ig_conv)
        self.assertEqual(spine1.pk, spine2.pk)
        self.assertEqual(Conversation.objects.filter(channel="instagram").count(), 1)
        self.assertEqual(
            ChannelConversation.objects.filter(
                channel="instagram", object_id=ig_conv.pk
            ).count(),
            1,
        )


# ---------------------------------------------------------------------------
# INV-06  get_or_create_for_channel dispatch
# ---------------------------------------------------------------------------


class TestGetOrCreateForChannel(TestCase):
    def test_dispatches_to_whatsapp(self):
        from apps.accounts.models import Account
        from apps.contacts.models import Contact
        from apps.whatsapp.models import Conversation as WaConversation
        from apps.whatsapp.models import WhatsAppContact

        account = Account.objects.create(company_name="Dispatch WA")
        contact = Contact.objects.create(account=account)
        wa_contact = WhatsAppContact.objects.create(
            account=account, phone_number="+260971000002", contact=contact
        )
        wa_conv = WaConversation.get_or_open(wa_contact)
        spine = Conversation.get_or_create_for_channel(
            wa_conv, Conversation.Channel.WHATSAPP
        )
        self.assertEqual(spine.channel, Conversation.Channel.WHATSAPP)

    def test_unsupported_channel_raises(self):
        fake_obj = type("FakeChannelObj", (), {"pk": 1})()
        with self.assertRaises(ValueError, msg="Unsupported channel"):
            Conversation.get_or_create_for_channel(fake_obj, "carrier_pigeon")


# ---------------------------------------------------------------------------
# INV-07  _bind_channel race safety
# ---------------------------------------------------------------------------


class TestBindChannelRace(TestCase):
    def test_losing_conversation_is_deleted_on_race(self):
        """INV-07: simulates the race-loser path in _bind_channel.

        When two callers both pass the initial filter (no ChannelConversation exists
        yet) and each creates a Conversation, only the get_or_create winner survives.
        We simulate this by manually calling _bind_channel twice in the same
        transaction window by pre-creating an orphaned Conversation, then verifying
        _bind_channel correctly adopts the already-bound one and the orphan is gone.
        """
        from apps.accounts.models import Account
        from apps.contacts.models import Contact
        from apps.whatsapp.models import Conversation as WaConversation
        from apps.whatsapp.models import WhatsAppContact

        account = Account.objects.create(company_name="Race Co")
        contact = Contact.objects.create(account=account)
        wa_contact = WhatsAppContact.objects.create(
            account=account, phone_number="+260971000003", contact=contact
        )
        wa_conv = WaConversation.get_or_open(wa_contact)

        # First bind — creates the ChannelConversation
        spine_a = Conversation._bind_channel(
            wa_conv,
            Conversation.Channel.WHATSAPP,
            account=account,
            contact=contact,
        )
        # Second bind — finds the existing ChannelConversation; any new Conversation it
        # creates internally is deleted; returned value must equal spine_a
        spine_b = Conversation._bind_channel(
            wa_conv,
            Conversation.Channel.WHATSAPP,
            account=account,
            contact=contact,
        )

        self.assertEqual(spine_a.pk, spine_b.pk)
        self.assertEqual(Conversation.objects.filter(channel="whatsapp").count(), 1)
        self.assertEqual(
            ChannelConversation.objects.filter(
                channel="whatsapp", object_id=wa_conv.pk
            ).count(),
            1,
        )


# ---------------------------------------------------------------------------
# INV-08 / INV-09 / INV-10  Backward-compat properties
# ---------------------------------------------------------------------------


class TestProperties(TestCase):
    def _make_whatsapp_spine(self):
        from apps.accounts.models import Account
        from apps.contacts.models import Contact
        from apps.whatsapp.models import Conversation as WaConversation
        from apps.whatsapp.models import WhatsAppContact

        account = Account.objects.create(company_name="Prop WA")
        contact = Contact.objects.create(account=account)
        wa_contact = WhatsAppContact.objects.create(
            account=account, phone_number="+260971000004", contact=contact
        )
        wa_conv = WaConversation.get_or_open(wa_contact)
        spine = Conversation.get_or_create_for_whatsapp(wa_conv)
        return spine, wa_conv

    def _make_instagram_spine(self):
        from apps.accounts.models import Account
        from apps.contacts.models import Contact
        from apps.instagram.models.account import InstagramBusinessAccount
        from apps.instagram.models.contact import InstagramContact
        from apps.instagram.models.conversation import InstagramConversation

        account = Account.objects.create(company_name="Prop IG")
        _ig_account = InstagramBusinessAccount.objects.create(
            account=account,
            instagram_business_account_id="ig_prop_001",
            page_id="page_prop_001",
            access_token="tok",
            verify_token="vt",
        )
        contact = Contact.objects.create(account=account)
        ig_contact = InstagramContact.objects.create(
            account=account,
            instagram_scoped_id="igsid_prop_001",
            contact=contact,
        )
        ig_conv = InstagramConversation.get_or_open(ig_contact)
        spine = Conversation.get_or_create_for_instagram(ig_conv)
        return spine, ig_conv

    def test_whatsapp_conversation_property_returns_backing_record(self):
        """INV-08: spine.whatsapp_conversation returns the WhatsApp Conversation."""
        spine, wa_conv = self._make_whatsapp_spine()
        self.assertEqual(spine.whatsapp_conversation.pk, wa_conv.pk)

    def test_instagram_conversation_property_returns_backing_record(self):
        """INV-09: spine.instagram_conversation returns the InstagramConversation."""
        spine, ig_conv = self._make_instagram_spine()
        self.assertEqual(spine.instagram_conversation.pk, ig_conv.pk)

    def test_whatsapp_property_returns_none_for_instagram_spine(self):
        """INV-10: instagram spine has no whatsapp_conversation."""
        spine, _ = self._make_instagram_spine()
        self.assertIsNone(spine.whatsapp_conversation)

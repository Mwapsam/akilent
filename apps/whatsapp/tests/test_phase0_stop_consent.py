"""Phase 0: a WhatsApp STOP/START is reflected on the canonical Contact - WhatsApp-only.

Contact.status is email subscription state and gates email audiences, so a WhatsApp
opt-out must not touch it (and an email unsubscribe must not opt out of WhatsApp).
"""
from unittest.mock import patch

from apps.contacts.models import Contact, ContactEvent
from apps.contacts.services import record_contact_event
from apps.whatsapp.models import WhatsAppContact
from apps.whatsapp.tests.test_auto_reply import PHONE, Base

REPLY = "apps.whatsapp.tasks._auto_reply_during_setup"


class StopConsentTest(Base):
    def setUp(self):
        super().setUp()
        p = patch(REPLY)
        p.start()
        self.addCleanup(p.stop)

    def contact(self):
        return Contact.objects.get(account=self.account, phone=PHONE)

    def events(self, kind):
        return ContactEvent.objects.filter(contact=self.contact(), type=kind)

    def test_stop_is_reflected_on_the_contact_without_touching_email_status(self):
        self.receive(text="Hi", msg_id="wamid.1")
        self.assertFalse(self.contact().whatsapp_opted_out)
        self.receive(text="STOP", msg_id="wamid.2")
        c = self.contact()
        self.assertTrue(c.whatsapp_opted_out)
        self.assertEqual(c.status, Contact.Status.SUBSCRIBED)      # email state untouched
        self.assertEqual(self.events("whatsapp.opted_out").count(), 1)

    def test_start_after_stop_reflects_opt_in(self):
        self.receive(text="STOP", msg_id="wamid.1")
        self.receive(text="START", msg_id="wamid.2")
        self.assertFalse(self.contact().whatsapp_opted_out)
        self.assertEqual(self.events("whatsapp.opted_in").count(), 1)

    def test_repeated_stop_records_one_event(self):
        self.receive(text="STOP", msg_id="wamid.1")
        self.receive(text="STOP", msg_id="wamid.2")
        self.assertEqual(self.events("whatsapp.opted_out").count(), 1)

    def test_opt_out_from_any_source_is_reflected(self):
        self.receive(text="Hi", msg_id="wamid.1")
        WhatsAppContact.objects.get(account=self.account, phone_number=PHONE).record_opt_out("api")
        self.assertTrue(self.contact().whatsapp_opted_out)

    def test_email_unsubscribe_does_not_opt_out_of_whatsapp(self):
        self.receive(text="Hi", msg_id="wamid.1")
        c = self.contact()
        record_contact_event(c, "email.unsubscribed")
        c.refresh_from_db()
        self.assertEqual(c.status, Contact.Status.UNSUBSCRIBED)
        self.assertFalse(c.whatsapp_opted_out)

    def test_identity_without_a_linked_contact_can_still_opt_out(self):  # regression
        wa = WhatsAppContact.objects.create(account=self.account, phone_number="+260961111111")
        wa.record_opt_out("keyword")
        wa.refresh_from_db()
        self.assertTrue(wa.is_opted_out)

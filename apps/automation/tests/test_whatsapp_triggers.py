"""Tests for the whatsapp.received Workflow trigger (apps/automation/triggers.py).

Covers: enrollment via a directly-linked Contact, enrollment via phone-match
backfill, Contact creation for a brand-new phone (the WhatsApp-first case),
identity stability across repeated messages from the same phone, and —
most importantly — that a failure in the new enrollment path never affects
the legacy AutomationRule dispatch that already runs on the same
MessageReceived event.
"""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun
from apps.contacts.models import Contact
from apps.core.events import MessageReceived, dispatcher
from apps.whatsapp.models import WhatsAppContact


def _wf(account, trigger_type, steps, name="WF"):
    return Workflow.objects.create(
        account=account, name=name, status=Workflow.Status.PUBLISHED,
        definition={"trigger": {"type": trigger_type}, "steps": steps},
    )


def _event(account, wa_contact, body="hello"):
    return MessageReceived(
        account_id=account.id,
        contact_id=wa_contact.id,
        message_id="wamid.123",
        channel="whatsapp",
        body=body,
        message_type="text",
        occurred_at=timezone.now(),
    )


class WhatsAppReceivedTriggerTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Acme", slug="acme-wt")

    def test_enrolls_when_whatsapp_contact_already_linked(self):
        contact = Contact.objects.create(account=self.account, email="a@example.com")
        wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260971234567", contact=contact,
        )
        wf = _wf(self.account, "whatsapp.received", [
            {"id": "a", "type": "stop"},
        ])

        dispatcher.publish(_event(self.account, wa_contact))

        run = WorkflowRun.objects.get(workflow=wf, contact=contact)
        self.assertIn(run.status, (WorkflowRun.Status.ACTIVE, WorkflowRun.Status.COMPLETED))
        self.assertEqual(run.context["message"]["body"], "hello")

    def test_enrolls_via_phone_match_backfill(self):
        contact = Contact.objects.create(
            account=self.account, email="b@example.com", phone="+260971234568",
        )
        wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260971234568",
        )
        _wf(self.account, "whatsapp.received", [{"id": "a", "type": "stop"}])

        dispatcher.publish(_event(self.account, wa_contact))

        wa_contact.refresh_from_db()
        self.assertEqual(wa_contact.contact_id, contact.id)
        self.assertTrue(WorkflowRun.objects.filter(contact=contact).exists())

    def test_unknown_phone_creates_contact_and_enrolls(self):
        """WhatsApp-first customer: no linked Contact and no phone match exists yet."""
        wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260971234569",
        )
        wf_received = _wf(self.account, "whatsapp.received", [{"id": "a", "type": "stop"}], name="WF received")
        wf_created = _wf(self.account, "contact.created", [{"id": "a", "type": "stop"}], name="WF created")

        dispatcher.publish(_event(self.account, wa_contact))

        wa_contact.refresh_from_db()
        self.assertIsNotNone(wa_contact.contact_id)
        contact = wa_contact.contact
        self.assertIsNone(contact.email)
        self.assertEqual(contact.phone, "+260971234569")

        # A brand-new phone fires both contact.created and whatsapp.received
        # for the same inbound message — intentional, not a bug.
        self.assertTrue(WorkflowRun.objects.filter(contact=contact, workflow=wf_received).exists())
        self.assertTrue(WorkflowRun.objects.filter(contact=contact, workflow=wf_created).exists())

    def test_second_message_from_same_phone_reuses_contact(self):
        """Identity must stabilize after the first interaction — no duplicate Contacts."""
        wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260971234571",
        )
        _wf(self.account, "whatsapp.received", [{"id": "a", "type": "stop"}])

        dispatcher.publish(_event(self.account, wa_contact, body="first"))
        wa_contact.refresh_from_db()
        first_contact_id = wa_contact.contact_id
        self.assertIsNotNone(first_contact_id)
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 1)

        dispatcher.publish(_event(self.account, wa_contact, body="second"))
        wa_contact.refresh_from_db()
        self.assertEqual(wa_contact.contact_id, first_contact_id)
        self.assertEqual(Contact.objects.filter(account=self.account).count(), 1)
        self.assertEqual(
            WorkflowRun.objects.filter(contact_id=first_contact_id).count(), 2,
        )

    def test_legacy_rule_dispatch_unaffected_by_workflow_enrollment_failure(self):
        """The core isolation invariant: a broken new path must not break the old one."""
        contact = Contact.objects.create(account=self.account, email="c@example.com")
        wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260971234570", contact=contact,
        )

        with patch(
            "apps.automation.triggers._enroll_workflows_for_reply",
            side_effect=RuntimeError("boom"),
        ), patch("apps.automation.tasks.evaluate_rules_for_message.delay") as mock_delay:
            dispatcher.publish(_event(self.account, wa_contact))

        mock_delay.assert_called_once()

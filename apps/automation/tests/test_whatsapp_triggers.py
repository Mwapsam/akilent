"""Tests for the whatsapp.received Workflow trigger (apps/automation/triggers.py).

Covers: enrollment via a directly-linked Contact, enrollment via phone-match
backfill, no-op when unlinked, and — most importantly — that a failure in the
new enrollment path never affects the legacy AutomationRule dispatch that
already runs on the same MessageReceived event.
"""
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.automation.models import Workflow, WorkflowRun
from apps.contacts.models import Contact
from apps.core.events import MessageReceived, dispatcher
from apps.whatsapp.models import WhatsAppContact


def _wf(account, trigger_type, steps):
    return Workflow.objects.create(
        account=account, name="WF", status=Workflow.Status.PUBLISHED,
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

    def test_no_enrollment_and_no_error_when_unlinked(self):
        wa_contact = WhatsAppContact.objects.create(
            account=self.account, phone_number="+260971234569",
        )
        _wf(self.account, "whatsapp.received", [{"id": "a", "type": "stop"}])

        try:
            dispatcher.publish(_event(self.account, wa_contact))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"publish() should not raise, but raised: {exc}")

        self.assertFalse(WorkflowRun.objects.exists())

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

"""Tenant isolation: no account can read another account's records.

Each test builds two accounts (A and B), creates a record under A, and
asserts that filtering by account B returns nothing — so a bug that drops
the account filter is caught immediately rather than silently leaking data.
"""

from django.test import TestCase

from apps.accounts.models import Account


def _account(slug):
    return Account.objects.create(company_name=slug, slug=slug)


class ContactIsolationTest(TestCase):
    def setUp(self):
        self.a = _account("tenant-a")
        self.b = _account("tenant-b")

    def test_contacts_scoped_to_account(self):
        from apps.contacts.models import Contact

        Contact.objects.create(
            account=self.a, first_name="Alice", phone="+260970000001"
        )
        assert Contact.objects.filter(account=self.b).count() == 0

    def test_conversations_scoped_to_account(self):
        from apps.whatsapp.models import Conversation, WhatsAppContact

        wa = WhatsAppContact.objects.create(
            account=self.a, phone_number="+260970000002"
        )
        Conversation.objects.create(account=self.a, contact=wa)
        assert Conversation.objects.filter(account=self.b).count() == 0

    def test_leads_scoped_to_account(self):
        from apps.contacts.models import Contact
        from apps.crm.models import Lead

        contact_a = Contact.objects.create(
            account=self.a, first_name="Alice", phone="+260970000003"
        )
        Lead.objects.create(account=self.a, contact=contact_a)
        assert Lead.objects.filter(account=self.b).count() == 0

    def test_insights_scoped_to_account(self):
        from apps.insights.models import Insight

        Insight.objects.create(
            account=self.a,
            type="inactive_customers",
            severity=Insight.Severity.WARNING,
            title="Test",
            body="Test body",
            evidence={"count": 1},
            suggested_action={"label": "Do something"},
        )
        assert Insight.objects.filter(account=self.b).count() == 0

    def test_platform_audit_logs_scoped_to_account(self):
        from apps.core.audit import platform_record
        from apps.core.models import PlatformAuditLog

        platform_record(
            action="team.invite", account=self.a, resource_id="test@example.com"
        )
        assert PlatformAuditLog.objects.filter(account=self.b).count() == 0
        assert PlatformAuditLog.objects.filter(account=self.a).count() == 1

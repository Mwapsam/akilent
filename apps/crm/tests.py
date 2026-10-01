"""CRM reliability tests — Phase 2 hardening."""

from unittest.mock import patch

from django.test import TestCase

from apps.accounts.models import Account
from apps.contacts.models import Contact
from apps.crm.models import Lead
from apps.crm.services import create_lead


class LeadConcurrentCreationTest(TestCase):
    """Concurrent create_lead calls for the same contact return the same Lead."""

    def setUp(self):
        self.account = Account.objects.create(company_name="CRM Co", slug="crm-co")
        self.contact = Contact.objects.create(
            account=self.account, first_name="Ana", phone="+260970000001"
        )

    def test_returns_existing_open_lead(self):
        lead1 = create_lead(self.account, self.contact)
        lead2 = create_lead(self.account, self.contact)
        assert lead1.pk == lead2.pk
        assert Lead.objects.filter(account=self.account).count() == 1

    def test_concurrent_race_returns_winner(self):
        # Simulate the race: the pre-check finds nothing, but the INSERT fails
        # because another worker already inserted. The service must recover by
        # returning the existing lead instead of raising.
        # Run inside transaction.atomic() to reproduce the production call path
        # (workflow_engine wraps in atomic); without it the aborted-transaction
        # InternalError that PostgreSQL raises on the recovery query wouldn't surface.
        from django.db import IntegrityError, transaction

        lead = create_lead(self.account, self.contact)

        with patch.object(Lead.objects, "create", side_effect=IntegrityError):
            with transaction.atomic():
                result = create_lead(self.account, self.contact)

        assert result.pk == lead.pk

    def test_lost_lead_does_not_block_new_open_lead(self):
        lead = create_lead(self.account, self.contact)
        lead.status = Lead.Status.LOST
        lead.save(update_fields=["status", "updated_at"])

        new_lead = create_lead(self.account, self.contact)
        assert new_lead.pk != lead.pk
        assert new_lead.status == Lead.Status.NEW

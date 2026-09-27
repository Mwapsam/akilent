"""Tests for apps.email.services.suppression module."""

import pytest
from django.db.models import F

from apps.accounts.models import Account
from apps.email.models import SuppressionListEntry, EmailMessage
from apps.email.services.suppression import (
    is_suppressed,
    is_suppressed_globally,
    get_suppressed_emails,
    record_event,
)
from apps.core.models import MailProviderSettings


@pytest.fixture
def account(db):
    """Create a test account."""
    return Account.objects.create(company_name="Test Account", slug="test")


@pytest.fixture
def email_domain(db, account):
    """Create a verified email domain."""
    from apps.email.models import EmailDomain
    return EmailDomain.objects.create(
        account=account,
        domain="example.com",
        status=EmailDomain.Status.VERIFIED,
    )


@pytest.fixture
def email_message(db, account, email_domain):
    """Create a test email message."""
    return EmailMessage.objects.create(
        account=account,
        domain=email_domain,
        from_email="sender@example.com",
        to_email="recipient@example.com",
        subject="Test",
    )


class TestIsSuppressed:
    """Test account-scoped suppression checking."""

    def test_not_suppressed(self, account):
        """Non-suppressed email should return False."""
        assert not is_suppressed(account, "unknown@example.com")

    def test_bounced_email(self, account):
        """Bounced email should be suppressed."""
        SuppressionListEntry.objects.create(
            account=account,
            email="bounced@example.com",
            reason=SuppressionListEntry.Reason.BOUNCE,
        )
        assert is_suppressed(account, "bounced@example.com")

    def test_soft_bounced_not_blocking(self, account):
        """Soft bounce alone should not be suppressed."""
        SuppressionListEntry.objects.create(
            account=account,
            email="soft@example.com",
            reason=SuppressionListEntry.Reason.SOFT_BOUNCE,
        )
        assert not is_suppressed(account, "soft@example.com")

    def test_unsubscribed_email(self, account):
        """Unsubscribed email should be suppressed."""
        SuppressionListEntry.objects.create(
            account=account,
            email="unsubscribed@example.com",
            reason=SuppressionListEntry.Reason.UNSUBSCRIBE,
        )
        assert is_suppressed(account, "unsubscribed@example.com")

    def test_complaint_email(self, account):
        """Complained email should be suppressed."""
        SuppressionListEntry.objects.create(
            account=account,
            email="complained@example.com",
            reason=SuppressionListEntry.Reason.COMPLAINT,
        )
        assert is_suppressed(account, "complained@example.com")

    def test_invalid_email(self, account):
        """Invalid email should be suppressed."""
        SuppressionListEntry.objects.create(
            account=account,
            email="invalid@example.com",
            reason=SuppressionListEntry.Reason.INVALID,
        )
        assert is_suppressed(account, "invalid@example.com")

    def test_account_scoped(self, account, db):
        """Suppression should be account-scoped."""
        other_account = Account.objects.create(company_name="Other", slug="other")

        SuppressionListEntry.objects.create(
            account=account,
            email="user@example.com",
            reason=SuppressionListEntry.Reason.BOUNCE,
        )

        # Suppressed in first account
        assert is_suppressed(account, "user@example.com")
        # Not suppressed in other account
        assert not is_suppressed(other_account, "user@example.com")


class TestIsSuppressedGlobally:
    """Test global suppression checking (cross-account)."""

    def test_not_suppressed_globally(self, db):
        """Non-suppressed email should return False globally."""
        assert not is_suppressed_globally("unknown@example.com")

    def test_globally_suppressed(self, db):
        """Email suppressed in any account should be globally suppressed."""
        account = Account.objects.create(company_name="Test", slug="test")
        SuppressionListEntry.objects.create(
            account=account,
            email="global@example.com",
            reason=SuppressionListEntry.Reason.BOUNCE,
        )
        assert is_suppressed_globally("global@example.com")

    def test_soft_bounce_not_globally_blocking(self, db):
        """Soft bounce should not globally suppress."""
        account = Account.objects.create(company_name="Test", slug="test")
        SuppressionListEntry.objects.create(
            account=account,
            email="soft@example.com",
            reason=SuppressionListEntry.Reason.SOFT_BOUNCE,
        )
        assert not is_suppressed_globally("soft@example.com")


class TestSystemMailSuppressionBoundary:
    """A tenant's unsubscribe is not a platform suppression.

    Regression: on 27 Sep an unsubscribe from Akilent's own test campaign
    silently stopped password resets to that address, because system mail
    honoured any tenant's UNSUBSCRIBE entry.
    """

    @pytest.mark.parametrize("reason", [
        SuppressionListEntry.Reason.UNSUBSCRIBE,
        SuppressionListEntry.Reason.MANUAL,
        SuppressionListEntry.Reason.INVALID,
    ])
    def test_tenant_consent_choices_do_not_block_system_mail(self, account, reason):
        SuppressionListEntry.objects.create(
            account=account, email="person@example.com", reason=reason
        )
        # Still blocked for that tenant's own mail...
        assert is_suppressed(account, "person@example.com")
        # ...but Akilent's account-critical mail goes through.
        assert not is_suppressed_globally("person@example.com")

    @pytest.mark.parametrize("reason", [
        SuppressionListEntry.Reason.BOUNCE,
        SuppressionListEntry.Reason.COMPLAINT,
    ])
    def test_tenant_hard_failures_do_block_system_mail(self, account, reason):
        SuppressionListEntry.objects.create(
            account=account, email="dead@example.com", reason=reason
        )
        assert is_suppressed_globally("dead@example.com")

    def test_global_suppression_blocks_system_mail(self, db):
        from apps.email.models import GlobalSuppression

        GlobalSuppression.objects.create(email="ghost@example.com", reason="bounce")
        assert is_suppressed_globally("ghost@example.com")

    def test_password_reset_reaches_a_tenant_unsubscriber(self, account, monkeypatch):
        """End to end through send_system_email, the path password resets use."""
        from apps.email.services.send import send_system_email
        from apps.email.types import SendResult

        SuppressionListEntry.objects.create(
            account=account, email="sam@example.com",
            reason=SuppressionListEntry.Reason.UNSUBSCRIBE,
        )
        sent = []

        class _Provider:
            def send(self, outbound):
                sent.append(outbound.to_email)
                return SendResult(success=True, provider_message_id="m-1")

        monkeypatch.setattr(
            "apps.email.providers.get_send_provider", lambda *a, **k: _Provider()
        )
        monkeypatch.setattr(
            "apps.email.services.validation.validate_recipient", lambda e: True
        )
        send_system_email("sam@example.com", "Reset your password", text_body="link")
        assert sent == ["sam@example.com"]


class TestGetSuppressedEmails:
    """Test bulk suppression lookup."""

    def test_empty_list(self, account):
        """Empty email list should return empty set."""
        assert get_suppressed_emails(account, []) == set()

    def test_all_valid(self, account):
        """All valid emails should return empty set."""
        result = get_suppressed_emails(account, [
            "user1@example.com",
            "user2@example.com",
        ])
        assert result == set()

    def test_some_suppressed(self, account):
        """Should return only suppressed emails."""
        SuppressionListEntry.objects.create(
            account=account,
            email="suppressed@example.com",
            reason=SuppressionListEntry.Reason.BOUNCE,
        )

        result = get_suppressed_emails(account, [
            "suppressed@example.com",
            "valid@example.com",
        ])
        assert result == {"suppressed@example.com"}

    def test_soft_bounce_not_included(self, account):
        """Soft bounces should not be included in suppressed set."""
        SuppressionListEntry.objects.create(
            account=account,
            email="soft@example.com",
            reason=SuppressionListEntry.Reason.SOFT_BOUNCE,
        )

        result = get_suppressed_emails(account, ["soft@example.com"])
        assert result == set()


class TestRecordEvent:
    """Test suppression event recording and upsert logic."""

    def test_new_bounce_entry(self, account):
        """New bounce should create entry with bounce_count=1."""
        entry = record_event(
            account=account,
            email="user@example.com",
            reason="bounce",
            bounce_type="Permanent",
        )
        assert entry.reason == SuppressionListEntry.Reason.BOUNCE
        assert entry.bounce_type == "Permanent"
        assert entry.bounce_count == 1

    def test_repeat_bounce_increments_count(self, account):
        """Second bounce should increment count and update fields."""
        entry1 = record_event(
            account=account,
            email="user@example.com",
            reason="bounce",
            bounce_type="Permanent",
        )
        assert entry1.bounce_count == 1

        entry2 = record_event(
            account=account,
            email="user@example.com",
            reason="bounce",
            bounce_type="Permanent",
        )
        assert entry2.bounce_count == 2
        assert entry2.pk == entry1.pk  # Same row

    def test_soft_bounce_escalation(self, account, db):
        """Soft bounces should escalate to hard after threshold."""
        settings = MailProviderSettings.load()
        settings.soft_bounce_threshold = 3
        settings.save()

        # First soft bounce
        entry1 = record_event(
            account=account,
            email="soft@example.com",
            reason="soft_bounce",
            bounce_type="Transient",
        )
        assert entry1.reason == SuppressionListEntry.Reason.SOFT_BOUNCE
        assert entry1.bounce_count == 1

        # Second soft bounce
        entry2 = record_event(
            account=account,
            email="soft@example.com",
            reason="soft_bounce",
            bounce_type="Transient",
        )
        assert entry2.reason == SuppressionListEntry.Reason.SOFT_BOUNCE
        assert entry2.bounce_count == 2

        # Third soft bounce should escalate to hard
        entry3 = record_event(
            account=account,
            email="soft@example.com",
            reason="soft_bounce",
            bounce_type="Transient",
        )
        assert entry3.reason == SuppressionListEntry.Reason.BOUNCE  # Escalated!
        assert entry3.bounce_count == 3

    def test_reason_update(self, account):
        """Recording a new reason should update the entry."""
        entry1 = record_event(
            account=account,
            email="user@example.com",
            reason="bounce",
        )
        assert entry1.reason == SuppressionListEntry.Reason.BOUNCE

        # Later complaint should update reason
        entry2 = record_event(
            account=account,
            email="user@example.com",
            reason="complaint",
        )
        assert entry2.reason == SuppressionListEntry.Reason.COMPLAINT
        assert entry2.bounce_count == 2

    def test_triggered_by_message(self, account, email_message):
        """Recording should set triggered_by_message when provided."""
        entry = record_event(
            account=account,
            email="user@example.com",
            reason="bounce",
            message=email_message,
        )
        assert entry.triggered_by_message == email_message

    def test_unsubscribe_overwrites_previous(self, account):
        """Unsubscribe should overwrite previous reason."""
        entry1 = record_event(
            account=account,
            email="user@example.com",
            reason="bounce",
        )

        entry2 = record_event(
            account=account,
            email="user@example.com",
            reason="unsubscribe",
        )

        assert entry2.reason == SuppressionListEntry.Reason.UNSUBSCRIBE
        assert entry2.bounce_count == 2

    def test_duplicate_constraint(self, account):
        """Unique constraint on (account, email) should prevent true duplicates."""
        # Recording same event twice should not violate unique constraint
        # because we use get_or_create + update internally
        entry1 = record_event(
            account=account,
            email="user@example.com",
            reason="bounce",
        )

        entry2 = record_event(
            account=account,
            email="user@example.com",
            reason="bounce",
        )

        assert SuppressionListEntry.objects.filter(
            account=account,
            email="user@example.com"
        ).count() == 1  # Only one row

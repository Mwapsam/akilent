"""Tenant isolation: no account can read another account's records.

Each test builds two accounts (A and B), creates a record under A, and
asserts that filtering by account B returns nothing — so a bug that drops
the account filter is caught immediately rather than silently leaking data.

Two test classes:
- ContactIsolationTest  — ORM-level queryset filtering
- ApiKeyTenantIsolationTest — HTTP-layer isolation via the public API
"""

import json
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Account


def _account(slug):
    return Account.objects.create(company_name=slug, slug=slug)


# ---------------------------------------------------------------------------
# Helpers shared by the HTTP-layer tests
# ---------------------------------------------------------------------------


def _make_account_with_api_key(slug):
    """Return (account, raw_api_key_string) for a fully subscribed account."""
    from django.contrib.auth.models import User

    from apps.accounts.models import Membership
    from apps.billing.models import Plan, Subscription
    from apps.email.models import EmailApiKey

    user = User.objects.create_user(slug, f"{slug}@example.com", "pw")
    acc = Account.objects.create(company_name=slug, slug=slug)
    Membership.objects.create(user=user, account=acc, role=Membership.Role.OWNER)
    # Use a per-account plan slug to avoid a UNIQUE-constraint race under pytest-xdist -n N,
    # where two workers can both pass the SELECT of get_or_create before either INSERT commits.
    plan, _ = Plan.objects.get_or_create(
        slug=f"test-plan-{slug}",
        defaults={
            "name": "Test",
            "price_monthly": Decimal("10"),
            "max_emails_per_month": 1000,
            "email_apis": True,
            "api_rate_per_min": 0,
        },
    )
    Subscription.objects.get_or_create(
        account=acc,
        defaults={
            "plan": plan,
            "status": Subscription.ACTIVE,
            "current_period_start": timezone.now(),
        },
    )
    _, raw = EmailApiKey.create_for_account(acc, name="k")
    return acc, raw


def _get(client, key, path):
    return client.get(f"/api/v1{path}", HTTP_X_API_KEY=key)


def _post(client, key, path, body):
    return client.post(
        f"/api/v1{path}",
        data=json.dumps(body),
        content_type="application/json",
        HTTP_X_API_KEY=key,
    )


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


# ---------------------------------------------------------------------------
# HTTP-layer tenant isolation: Account B's API key must not read Account A's
# data through the public REST API. These tests catch view-level bugs that
# ORM-level tests miss (e.g. a view that forgets to scope its queryset).
#
# Written as a TestCase subclass (not bare pytest functions) so they are
# discovered by both pytest and manage.py test.
# ---------------------------------------------------------------------------


class ApiKeyTenantIsolationTest(TestCase):
    def test_api_contacts_list_is_scoped_to_own_account(self):
        acc_a, key_a = _make_account_with_api_key("http-a")
        acc_b, key_b = _make_account_with_api_key("http-b")

        # Create a contact under Account A
        r = _post(self.client, key_a, "/contacts", {"email": "alice@acme.test"})
        assert r.status_code == 201
        contact_id = r.json()["id"]

        # Account B's list must be empty
        lst = _get(self.client, key_b, "/contacts")
        assert lst.status_code == 200
        assert lst.json()["total"] == 0, "Account B must not see Account A's contacts"

        # Account B must not fetch Account A's contact by ID
        detail = _get(self.client, key_b, f"/contacts/{contact_id}")
        assert detail.status_code in (403, 404), (
            "Account B must not access Account A's contact by ID"
        )

    def test_api_contact_patch_blocked_across_tenants(self):
        acc_a, key_a = _make_account_with_api_key("patch-a")
        acc_b, key_b = _make_account_with_api_key("patch-b")

        r = _post(self.client, key_a, "/contacts", {"email": "bob@acme.test"})
        assert r.status_code == 201
        contact_id = r.json()["id"]

        patch = self.client.patch(
            f"/api/v1/contacts/{contact_id}",
            data=json.dumps({"first_name": "Hijacked"}),
            content_type="application/json",
            HTTP_X_API_KEY=key_b,
        )
        assert patch.status_code in (403, 404), (
            "Account B must not modify Account A's contact"
        )

        # Verify the name was not changed
        detail = _get(self.client, key_a, f"/contacts/{contact_id}")
        assert detail.json().get("first_name") != "Hijacked"

    def test_api_contact_list_does_not_leak_across_tenants(self):
        """Two accounts both have contacts — each sees only their own."""
        acc_a, key_a = _make_account_with_api_key("leak-a")
        acc_b, key_b = _make_account_with_api_key("leak-b")

        _post(self.client, key_a, "/contacts", {"email": "a1@acme.test"})
        _post(self.client, key_a, "/contacts", {"email": "a2@acme.test"})
        _post(self.client, key_b, "/contacts", {"email": "b1@acme.test"})

        lst_a = _get(self.client, key_a, "/contacts")
        lst_b = _get(self.client, key_b, "/contacts")

        assert lst_a.json()["total"] == 2, (
            "Account A should see exactly its own 2 contacts"
        )
        assert lst_b.json()["total"] == 1, (
            "Account B should see exactly its own 1 contact"
        )

    def test_api_requires_valid_api_key(self):
        """Requests with no key or a garbage key must be rejected."""
        r_no_key = self.client.get("/api/v1/contacts")
        assert r_no_key.status_code in (401, 403)

        r_bad_key = self.client.get("/api/v1/contacts", HTTP_X_API_KEY="garbage-key")
        assert r_bad_key.status_code in (401, 403)

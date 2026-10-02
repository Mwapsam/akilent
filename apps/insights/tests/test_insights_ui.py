"""Tests for the insights UI: intelligence panel display, acknowledge, dismiss."""

from django.contrib.auth.models import User
from django.test import Client, TestCase

from apps.accounts.models import Account, Membership
from apps.insights.models import Insight


def _make_account(slug):
    user = User.objects.create_user(
        username=f"{slug}@x.com", email=f"{slug}@x.com", password="pass"
    )
    account = Account.objects.create(
        company_name="Test Corp", slug=slug, selected_services="whatsapp"
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account, user


def _make_insight(account, **kwargs):
    defaults = dict(
        type="test_type",
        severity=Insight.Severity.WARNING,
        title="Test insight",
        body="Something needs attention.",
        evidence_count=5,
        suggested_action={"action": "inbox", "label": "Open inbox"},
    )
    defaults.update(kwargs)
    return Insight.objects.create(account=account, **defaults)


class InsightsPageTest(TestCase):
    def setUp(self):
        self.account, self.user = _make_account("insights-ui")
        self.client = Client()
        session = self.client.session
        session["account_slug"] = self.account.slug
        session.save()
        self.client.force_login(self.user)

    def test_page_loads_without_insights(self):
        resp = self.client.get("/insights/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"intelligence-h", resp.content)

    def test_new_insight_appears_on_page(self):
        _make_insight(self.account, title="You have 3 inactive customers")
        resp = self.client.get("/insights/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"You have 3 inactive customers", resp.content)

    def test_dismissed_insight_not_shown(self):
        _make_insight(
            self.account,
            title="Hidden insight",
            status=Insight.Status.DISMISSED,
        )
        resp = self.client.get("/insights/")
        self.assertNotIn(b"Hidden insight", resp.content)

    def test_urgent_badge_present(self):
        _make_insight(
            self.account,
            title="Urgent situation",
            severity=Insight.Severity.URGENT,
        )
        resp = self.client.get("/insights/")
        self.assertIn(b"Urgent", resp.content)

    def test_insight_evidence_count_shown(self):
        _make_insight(self.account, evidence_count=12, title="Evidence insight")
        resp = self.client.get("/insights/")
        self.assertIn(b"12 records", resp.content)


class InsightAcknowledgeTest(TestCase):
    def setUp(self):
        self.account, self.user = _make_account("ack-ui")
        self.client = Client()
        session = self.client.session
        session["account_slug"] = self.account.slug
        session.save()
        self.client.force_login(self.user)

    def test_acknowledge_sets_status(self):
        insight = _make_insight(self.account)
        resp = self.client.post(f"/insights/{insight.pk}/acknowledge/")
        self.assertRedirects(resp, "/insights/", fetch_redirect_response=False)
        insight.refresh_from_db()
        self.assertEqual(insight.status, Insight.Status.ACKNOWLEDGED)

    def test_acknowledge_wrong_account_is_noop(self):
        other_account, _ = _make_account("other-ack")
        insight = _make_insight(other_account)
        self.client.post(f"/insights/{insight.pk}/acknowledge/")
        insight.refresh_from_db()
        self.assertEqual(insight.status, Insight.Status.NEW)

    def test_acknowledge_already_dismissed_is_noop(self):
        insight = _make_insight(self.account, status=Insight.Status.DISMISSED)
        self.client.post(f"/insights/{insight.pk}/acknowledge/")
        insight.refresh_from_db()
        self.assertEqual(insight.status, Insight.Status.DISMISSED)


class InsightDismissTest(TestCase):
    def setUp(self):
        self.account, self.user = _make_account("dismiss-ui")
        self.client = Client()
        session = self.client.session
        session["account_slug"] = self.account.slug
        session.save()
        self.client.force_login(self.user)

    def test_dismiss_new_insight(self):
        insight = _make_insight(self.account)
        resp = self.client.post(f"/insights/{insight.pk}/dismiss/")
        self.assertRedirects(resp, "/insights/", fetch_redirect_response=False)
        insight.refresh_from_db()
        self.assertEqual(insight.status, Insight.Status.DISMISSED)

    def test_dismiss_acknowledged_insight(self):
        insight = _make_insight(self.account, status=Insight.Status.ACKNOWLEDGED)
        self.client.post(f"/insights/{insight.pk}/dismiss/")
        insight.refresh_from_db()
        self.assertEqual(insight.status, Insight.Status.DISMISSED)

    def test_dismiss_wrong_account_is_noop(self):
        other_account, _ = _make_account("other-dismiss")
        insight = _make_insight(other_account)
        self.client.post(f"/insights/{insight.pk}/dismiss/")
        insight.refresh_from_db()
        self.assertEqual(insight.status, Insight.Status.NEW)

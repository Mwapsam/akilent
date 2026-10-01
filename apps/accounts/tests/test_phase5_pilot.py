"""Phase 5 pilot enrollment tests.

Tests cover setup_score(), enroll_pilot(), pilot_outcome(), and the
enroll_pilot management command.
"""

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase


def _make_account(slug, company="Test Corp"):
    from apps.accounts.models import Account, Membership

    user = User.objects.create_user(
        username=f"{slug}@x.com", email=f"{slug}@x.com", password="x"
    )
    account = Account.objects.create(
        company_name=company, slug=slug, selected_services="whatsapp"
    )
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    return account, user


def _fill_business_context(account):
    """Populate BusinessContext with all scored fields (+40 pts)."""
    from apps.accounts.models import BusinessContext

    ctx, _ = BusinessContext.objects.get_or_create(account=account)
    ctx.business_model = "b2c"
    ctx.customer_channels = ["whatsapp"]
    ctx.objectives = ["increase_sales"]
    ctx.capability_profile = {"increase_sales": ["sales", "follow_ups", "orders"]}
    ctx.save()
    return ctx


def _fill_business_knowledge(account):
    """Populate BusinessKnowledge with all scored fields (+30 pts)."""
    from apps.accounts.models import BusinessKnowledge

    knowledge, _ = BusinessKnowledge.objects.get_or_create(account=account)
    knowledge.who_is_it_for = "Small business owners"
    knowledge.problem_solved = "Managing customer conversations"
    knowledge.common_questions = [{"q": "How do I sign up?", "a": "Visit our website."}]
    knowledge.faqs = [{"q": "Is there a free trial?", "a": "Yes, 14 days."}]
    knowledge.eligibility_rules = {"min_age": 18}
    knowledge.save()
    return knowledge


def _fill_business_profile(account):
    """Populate BusinessProfile with scored fields (+10 pts)."""
    from apps.accounts.models import BusinessProfile

    profile, _ = BusinessProfile.objects.get_or_create(account=account)
    profile.what_you_sell = "CRM software"
    profile.location = "Lusaka, Zambia"
    profile.save()
    return profile


def _add_whatsapp_number(account):
    """Add a WhatsApp business number (+10 pts)."""
    from apps.whatsapp.models import WhatsAppBusinessNumber

    return WhatsAppBusinessNumber.objects.create(
        account=account,
        phone_number_id="1234567890",
        waba_id="waba-001",
        display_number="+260971000001",
        access_token="tok",
    )


def _add_completed_campaign(account, user):
    """Add a completed campaign (+5 pts)."""
    from django.utils import timezone

    from apps.contacts.models import Contact, ContactList
    from apps.whatsapp.models import MessageTemplate, WhatsAppCampaign

    contact = Contact.objects.create(account=account, phone="+260977500001", source="seed")
    clist = ContactList.objects.create(account=account, name="Pilot List", slug=f"pilot-list-{account.slug}")
    clist.contacts.set([contact])
    template = MessageTemplate.objects.create(
        account=account,
        name="pilot_tpl",
        whatsapp_template_name="pilot_tpl",
        approval_status=MessageTemplate.ApprovalStatus.APPROVED,
        category="MARKETING",
        language_code="en",
    )
    return WhatsAppCampaign.objects.create(
        account=account,
        name="Pilot Campaign",
        contact_list=clist,
        template=template,
        status=WhatsAppCampaign.Status.COMPLETED,
        completed_at=timezone.now(),
    )


def _add_second_member(account):
    """Add a second member to the account (+5 pts for team size ≥ 2)."""
    from apps.accounts.models import Membership

    user2 = User.objects.create_user(
        username=f"member-{account.slug}@x.com",
        email=f"member-{account.slug}@x.com",
        password="x",
    )
    Membership.objects.create(user=user2, account=account, role=Membership.Role.MEMBER)
    return user2


class SetupScoreTest(TestCase):
    """setup_score() returns accurate scores for each component."""

    def setUp(self):
        self.account, self.user = _make_account("score-test")

    def test_empty_account_scores_zero(self):
        from apps.accounts.pilot import setup_score

        self.assertEqual(setup_score(self.account), 0)

    def test_business_context_adds_40_points(self):
        from apps.accounts.pilot import setup_score

        _fill_business_context(self.account)
        score = setup_score(self.account)
        self.assertEqual(score, 40)

    def test_business_knowledge_adds_30_points(self):
        from apps.accounts.pilot import setup_score

        _fill_business_knowledge(self.account)
        score = setup_score(self.account)
        self.assertEqual(score, 30)

    def test_full_setup_scores_100(self):
        from apps.accounts.pilot import setup_score

        _fill_business_context(self.account)
        _fill_business_knowledge(self.account)
        _fill_business_profile(self.account)
        _add_whatsapp_number(self.account)
        _add_completed_campaign(self.account, self.user)
        _add_second_member(self.account)

        score = setup_score(self.account)
        self.assertEqual(score, 100)

    def test_score_capped_at_100(self):
        from apps.accounts.pilot import setup_score

        _fill_business_context(self.account)
        _fill_business_knowledge(self.account)
        _fill_business_profile(self.account)
        _add_whatsapp_number(self.account)
        _add_completed_campaign(self.account, self.user)
        _add_second_member(self.account)

        self.assertLessEqual(setup_score(self.account), 100)


class EnrollPilotTest(TestCase):
    """enroll_pilot() gates on ≥80 and snapshots baseline metrics."""

    def setUp(self):
        self.account, self.user = _make_account("enroll-test")

    def test_low_score_raises_value_error(self):
        from apps.accounts.pilot import enroll_pilot

        with self.assertRaises(ValueError) as cm:
            enroll_pilot(self.account)
        self.assertIn("setup score", str(cm.exception))

    def test_high_score_creates_enrollment(self):
        from apps.accounts.models import PilotEnrollment
        from apps.accounts.pilot import enroll_pilot

        _fill_business_context(self.account)
        _fill_business_knowledge(self.account)
        _fill_business_profile(self.account)
        _add_whatsapp_number(self.account)
        _add_completed_campaign(self.account, self.user)
        _add_second_member(self.account)

        enrollment = enroll_pilot(self.account, wave=2, enrolled_by=self.user)
        self.assertIsNotNone(enrollment.pk)
        self.assertEqual(enrollment.wave, 2)
        self.assertEqual(enrollment.enrolled_by, self.user)
        self.assertTrue(PilotEnrollment.objects.filter(account=self.account).exists())

    def test_re_enrollment_updates_existing_row(self):
        from apps.accounts.models import PilotEnrollment
        from apps.accounts.pilot import enroll_pilot

        _fill_business_context(self.account)
        _fill_business_knowledge(self.account)
        _fill_business_profile(self.account)
        _add_whatsapp_number(self.account)
        _add_completed_campaign(self.account, self.user)
        _add_second_member(self.account)

        enroll_pilot(self.account, wave=2)
        enroll_pilot(self.account, wave=3)

        self.assertEqual(PilotEnrollment.objects.filter(account=self.account).count(), 1)
        enrollment = PilotEnrollment.objects.get(account=self.account)
        self.assertEqual(enrollment.wave, 3)


class PilotOutcomeTest(TestCase):
    """pilot_outcome() returns the right shape for enrolled and unenrolled accounts."""

    def setUp(self):
        self.account, self.user = _make_account("outcome-test")

    def test_unenrolled_account_returns_enrolled_false(self):
        from apps.accounts.pilot import pilot_outcome

        result = pilot_outcome(self.account)
        self.assertFalse(result["enrolled"])

    def test_enrolled_account_returns_expected_keys(self):
        from apps.accounts.pilot import enroll_pilot, pilot_outcome

        _fill_business_context(self.account)
        _fill_business_knowledge(self.account)
        _fill_business_profile(self.account)
        _add_whatsapp_number(self.account)
        _add_completed_campaign(self.account, self.user)
        _add_second_member(self.account)

        enroll_pilot(self.account, wave=2)
        result = pilot_outcome(self.account)

        self.assertTrue(result["enrolled"])
        self.assertEqual(result["wave"], 2)
        self.assertIn("setup_score", result)
        self.assertIn("baseline_lead_count", result)
        self.assertIn("current_lead_count", result)
        self.assertIn("leads_added", result)


class EnrollPilotCommandTest(TestCase):
    """manage.py enroll_pilot command integration tests."""

    def setUp(self):
        self.account, self.user = _make_account("cmd-enroll")

    def _run(self, *args, **kwargs):
        out, err = StringIO(), StringIO()
        code = 0
        try:
            call_command("enroll_pilot", *args, stdout=out, stderr=err, **kwargs)
        except SystemExit as exc:
            code = exc.code
        return out.getvalue(), err.getvalue(), code

    def test_unknown_account_exits_1(self):
        _, err, code = self._run("--account", "no-such-slug")
        self.assertEqual(code, 1)
        self.assertIn("No active account", err)

    def test_low_score_account_exits_1(self):
        _, err, code = self._run("--account", self.account.slug)
        self.assertEqual(code, 1)
        self.assertIn("setup score", err)

    def test_dry_run_does_not_enroll(self):
        from apps.accounts.models import PilotEnrollment

        _fill_business_context(self.account)
        _fill_business_knowledge(self.account)
        _fill_business_profile(self.account)
        _add_whatsapp_number(self.account)
        _add_completed_campaign(self.account, self.user)
        _add_second_member(self.account)

        out, _, code = self._run("--account", self.account.slug, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("100", out)
        self.assertFalse(PilotEnrollment.objects.filter(account=self.account).exists())

    def test_full_score_enrolls_successfully(self):
        from apps.accounts.models import PilotEnrollment

        _fill_business_context(self.account)
        _fill_business_knowledge(self.account)
        _fill_business_profile(self.account)
        _add_whatsapp_number(self.account)
        _add_completed_campaign(self.account, self.user)
        _add_second_member(self.account)

        out, _, code = self._run("--account", self.account.slug, "--wave", "2")
        self.assertEqual(code, 0)
        self.assertTrue(PilotEnrollment.objects.filter(account=self.account).exists())


class PilotReportCommandTest(TestCase):
    """manage.py pilot_report prints per-pilot outcome rows."""

    def setUp(self):
        self.account, self.user = _make_account("cmd-report")
        _fill_business_context(self.account)
        _fill_business_knowledge(self.account)
        _fill_business_profile(self.account)
        _add_whatsapp_number(self.account)
        _add_completed_campaign(self.account, self.user)
        _add_second_member(self.account)

        from apps.accounts.pilot import enroll_pilot

        enroll_pilot(self.account, wave=2)

    def _run(self, *args, **kwargs):
        out = StringIO()
        call_command("pilot_report", *args, stdout=out, **kwargs)
        return out.getvalue()

    def test_report_includes_account_slug(self):
        output = self._run()
        self.assertIn(self.account.slug, output)

    def test_report_filtered_by_wave(self):
        output = self._run("--wave", "2")
        self.assertIn(self.account.slug, output)

    def test_report_filtered_by_wrong_wave_shows_nothing(self):
        output = self._run("--wave", "99")
        self.assertIn("No pilot enrollments found", output)

    def test_report_shows_setup_score(self):
        output = self._run("--account", self.account.slug)
        self.assertIn("100", output)

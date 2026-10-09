"""Recommended moderation rules: added once, editable, never duplicated."""

from unittest.mock import patch

from django.test import TestCase

from apps.billing.models import AccountFeatureOverride
from apps.instagram.models import ModerationRule
from apps.instagram.services.default_rules import (
    DEFAULT_MODERATION_RULES,
    add_default_moderation_rules,
)
from apps.instagram.services.moderation import moderate_comment
from apps.instagram.tests.helpers import (
    make_account,
    make_instagram_account,
    make_instagram_contact,
)
from apps.instagram.tests.test_phase2_acceptance import make_thread_and_comment


class DefaultRulesServiceTest(TestCase):
    def setUp(self):
        self.account, _ = make_account()

    def test_adds_all_recommended_rules_once(self):
        self.assertEqual(
            add_default_moderation_rules(self.account), len(DEFAULT_MODERATION_RULES)
        )
        self.assertEqual(add_default_moderation_rules(self.account), 0)
        self.assertEqual(
            ModerationRule.objects.filter(account=self.account).count(),
            len(DEFAULT_MODERATION_RULES),
        )

    def test_only_if_none_leaves_existing_moderation_alone(self):
        ModerationRule.objects.create(
            account=self.account,
            name="Mine",
            match_type=ModerationRule.MatchType.KEYWORD,
            keywords="x",
            moderation_action=ModerationRule.ModerationAction.HIDE,
        )
        self.assertEqual(
            add_default_moderation_rules(self.account, only_if_none=True), 0
        )

    def test_only_missing_rules_are_added(self):
        add_default_moderation_rules(self.account)
        ModerationRule.objects.get(account=self.account, name="Complaints").delete()
        self.assertEqual(add_default_moderation_rules(self.account), 1)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_spam_default_hides_scam_comments(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = (True, "")
        add_default_moderation_rules(self.account)
        ig_account = make_instagram_account(self.account)
        ig_contact, _ = make_instagram_contact(self.account, ig_account)
        _, comment = make_thread_and_comment(
            ig_account, ig_contact, "Earn money with bitcoin, whatsapp me"
        )
        log = moderate_comment(comment)
        self.assertEqual(log.rule.name, "Spam and scams")
        self.assertEqual(log.moderation_action, ModerationRule.ModerationAction.HIDE)


class DefaultRulesPageTest(TestCase):
    def setUp(self):
        self.account, self.user = make_account()
        make_instagram_account(self.account)
        AccountFeatureOverride.objects.create(
            account=self.account, key="instagram", grant=True, note="test"
        )
        self.client.force_login(self.user)

    def test_button_adds_rules_that_stay_editable(self):
        self.assertContains(
            self.client.get("/instagram/rules/"), "Add recommended rules"
        )
        response = self.client.post("/instagram/rules/moderation/recommended/")
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/instagram/rules/")
        self.assertContains(page, "Spam and scams")
        self.assertNotContains(page, "Add recommended rules")

        rule = ModerationRule.objects.get(account=self.account, name="Abusive language")
        response = self.client.post(
            f"/instagram/rules/moderation/{rule.pk}/edit/",
            {
                "name": "Abusive language",
                "match_type": "toxicity",
                "keywords": "",
                "moderation_action": "hide",
                "automation_trigger": "none",
                "priority": 20,
            },
        )
        self.assertEqual(response.status_code, 302)
        rule.refresh_from_db()
        self.assertEqual(rule.moderation_action, ModerationRule.ModerationAction.HIDE)

    def test_connecting_an_account_adds_the_defaults_once(self):
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        from apps.instagram.views import _finish_instagram_oauth

        other, user = make_account()
        chosen = {
            "instagram_business_account_id": "17841499999999999",
            "page_id": "",
            "page_access_token": "IGAAtoken",
            "username": "shop",
        }
        for _ in range(2):  # connect, then reconnect
            request = RequestFactory().get("/")
            request.user = user
            request.session = self.client.session
            request._messages = FallbackStorage(request)
            ok, _err = _finish_instagram_oauth(
                request, other, chosen, subscribe_fn=lambda *a: True
            )
            self.assertTrue(ok)
        self.assertEqual(
            ModerationRule.objects.filter(account=other).count(),
            len(DEFAULT_MODERATION_RULES),
        )

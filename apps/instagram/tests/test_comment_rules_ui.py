"""The business-facing comment rules pages (private replies + moderation)."""

from unittest.mock import patch

from django.test import TestCase

from apps.billing.models import AccountFeatureOverride
from apps.instagram.models import CommentTrigger, ModerationRule
from apps.instagram.tests.helpers import make_account, make_instagram_account


class CommentRulesPagesTest(TestCase):
    def setUp(self):
        self.account, self.user = make_account()
        make_instagram_account(self.account)
        AccountFeatureOverride.objects.create(
            account=self.account, key="instagram", grant=True, note="test"
        )
        self.client.force_login(self.user)

    def test_page_is_locked_without_the_instagram_feature(self):
        AccountFeatureOverride.objects.filter(account=self.account).delete()
        response = self.client.get("/instagram/rules/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/billing/locked/instagram/", response.url)

    def test_create_reply_rule(self):
        response = self.client.post(
            "/instagram/rules/reply/new/",
            {
                "name": "Price",
                "match_type": "keyword",
                "keywords": "price, how much",
                "reply_template": "Thanks {username}! Price sent in DM.",
                "priority": 10,
            },
        )
        self.assertEqual(response.status_code, 302)
        rule = CommentTrigger.objects.get(account=self.account)
        self.assertEqual(rule.keyword_list(), ["price", "how much"])
        self.assertContains(self.client.get("/instagram/rules/"), "Price")

    def test_keyword_rule_needs_keywords(self):
        response = self.client.post(
            "/instagram/rules/reply/new/",
            {
                "name": "x",
                "match_type": "keyword",
                "keywords": "",
                "reply_template": "hi",
                "priority": 10,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add at least one keyword.")
        self.assertFalse(CommentTrigger.objects.exists())

    def test_moderation_rule_must_do_something(self):
        response = self.client.post(
            "/instagram/rules/moderation/new/",
            {
                "name": "noop",
                "match_type": "spam_detection",
                "moderation_action": "none",
                "automation_trigger": "none",
                "priority": 10,
            },
        )
        self.assertContains(response, "Choose at least one thing")
        self.assertFalse(ModerationRule.objects.exists())

    def test_pause_and_delete(self):
        rule = CommentTrigger.objects.create(
            account=self.account,
            name="Any",
            match_type="any_comment",
            reply_template="Hi",
        )
        self.client.post(f"/instagram/rules/reply/{rule.pk}/toggle/")
        rule.refresh_from_db()
        self.assertFalse(rule.is_active)
        self.client.post(f"/instagram/rules/reply/{rule.pk}/delete/")
        self.assertFalse(CommentTrigger.objects.exists())

    def test_another_business_rule_is_not_reachable(self):
        other, _ = make_account()
        rule = CommentTrigger.objects.create(
            account=other, name="Theirs", match_type="any_comment", reply_template="Hi"
        )
        response = self.client.post(f"/instagram/rules/reply/{rule.pk}/delete/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(CommentTrigger.objects.filter(pk=rule.pk).exists())

    def test_saved_rule_fires_on_a_matching_comment(self):
        from apps.instagram.providers.base import SendResult
        from apps.instagram.services.inbound import _process_comment_entry

        self.client.post(
            "/instagram/rules/reply/new/",
            {
                "name": "Price",
                "match_type": "keyword",
                "keywords": "price",
                "reply_template": "Sent you the price!",
                "priority": 10,
            },
        )
        ig_account = self.account.instagram_accounts.get()
        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider.private_reply",
            return_value=SendResult(success=True, provider_message_id="mid.pr1"),
        ) as private_reply:
            _process_comment_entry(
                ig_account,
                {
                    "id": "c1",
                    "text": "price please?",
                    "from": {"id": "igsid_x", "username": "ann"},
                    "media": {"id": "post1"},
                },
            )
        private_reply.assert_called_once_with("c1", "Sent you the price!")

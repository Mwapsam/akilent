"""Comment moderation actually does something, once, and the business can see it."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.billing.models import AccountFeatureOverride
from apps.instagram.models.comment import Comment
from apps.instagram.models.moderation import ModerationLog, ModerationRule
from apps.instagram.providers.meta import MetaInstagramProvider
from apps.instagram.services.moderation import moderate_comment

from .helpers import make_account, make_instagram_account, make_instagram_contact
from .test_phase2_acceptance import make_rule, make_thread_and_comment

SEND = "apps.email.services.send.send_system_email"


def _response(ok: bool, payload: dict | None = None, status: int = 200):
    resp = MagicMock(ok=ok, status_code=status, text="")
    resp.json.return_value = payload or {}
    return resp


class ProviderCommentCallsTest(TestCase):
    def setUp(self):
        self.provider = MetaInstagramProvider("IGAAtoken", "17841400000000000")

    @patch("apps.instagram.providers.meta.requests.request")
    def test_hide_sends_hide_true_as_a_query_param(self, mock_request):
        mock_request.return_value = _response(True, {"success": True})
        self.assertEqual(self.provider.hide_comment("c1"), (True, ""))
        method, url = mock_request.call_args.args
        self.assertEqual(method, "post")
        self.assertTrue(url.endswith("/c1"))
        self.assertEqual(mock_request.call_args.kwargs["params"], {"hide": "true"})
        self.assertNotIn("json", mock_request.call_args.kwargs)

    @patch("apps.instagram.providers.meta.requests.request")
    def test_failure_returns_metas_error(self, mock_request):
        mock_request.return_value = _response(
            False,
            {"error": {"code": 10, "message": "Application does not have permission"}},
            status=403,
        )
        ok, error = self.provider.delete_comment("c1")
        self.assertFalse(ok)
        self.assertIn("Application does not have permission", error)
        self.assertEqual(mock_request.call_args.args[0], "delete")


class ModerationBehaviourTest(TestCase):
    def setUp(self):
        patcher = patch(SEND)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        self.account, self.user = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)

    def test_flag_marks_flagged_and_emails_owners(self):
        make_rule(
            self.account,
            name="Complaints",
            keywords="refund",
            moderation_action=ModerationRule.ModerationAction.FLAG,
        )
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "I want a refund"
        )
        moderate_comment(comment)
        comment.refresh_from_db()
        self.assertEqual(comment.moderation_state, Comment.ModerationState.FLAGGED)
        self.send.assert_called_once()
        email = self.send.call_args.kwargs
        self.assertEqual(email["to_email"], self.user.email)
        self.assertIn("I want a refund", email["text_body"])
        self.assertIn("Complaints", email["text_body"])

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_reprocessing_does_not_act_or_notify_twice(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = (True, "")
        make_rule(
            self.account,
            keywords="spam",
            moderation_action=ModerationRule.ModerationAction.HIDE,
            automation_trigger=ModerationRule.AutomationTrigger.NOTIFY_STAFF,
        )
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "spam spam"
        )
        self.assertIsNotNone(moderate_comment(comment))
        self.assertIsNone(moderate_comment(comment))
        self.assertEqual(ModerationLog.objects.filter(comment=comment).count(), 1)
        self.assertEqual(MockProvider.return_value.hide_comment.call_count, 1)
        self.send.assert_called_once()

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_failed_hide_tells_the_team_why(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = (
            False,
            "[10] Application does not have permission for this action",
        )
        make_rule(
            self.account,
            keywords="spam",
            moderation_action=ModerationRule.ModerationAction.HIDE,
            automation_trigger=ModerationRule.AutomationTrigger.NOTIFY_STAFF,
        )
        _, comment = make_thread_and_comment(self.ig_account, self.ig_contact, "spam")
        log = moderate_comment(comment)
        self.assertEqual(log.outcome, ModerationLog.Outcome.FAILED)
        self.assertIn(
            "does not have permission", self.send.call_args.kwargs["text_body"]
        )


class ModerationActivityPageTest(TestCase):
    def setUp(self):
        self.account, self.user = make_account()
        self.ig_account = make_instagram_account(self.account)
        self.ig_contact, _ = make_instagram_contact(self.account, self.ig_account)
        AccountFeatureOverride.objects.create(
            account=self.account, key="instagram", grant=True, note="test"
        )
        self.client.force_login(self.user)

    @patch("apps.instagram.providers.meta.MetaInstagramProvider")
    def test_rules_page_shows_results_and_errors(self, MockProvider):
        MockProvider.return_value.hide_comment.return_value = (
            False,
            "[10] Application does not have permission for this action",
        )
        make_rule(
            self.account,
            name="Spam filter",
            keywords="spam",
            moderation_action=ModerationRule.ModerationAction.HIDE,
        )
        _, comment = make_thread_and_comment(
            self.ig_account, self.ig_contact, "buy spam here"
        )
        moderate_comment(comment)

        response = self.client.get("/instagram/rules/")
        self.assertContains(response, "Recent moderation activity")
        self.assertContains(response, "buy spam here")
        self.assertContains(response, "Spam filter")
        self.assertContains(response, "does not have permission")

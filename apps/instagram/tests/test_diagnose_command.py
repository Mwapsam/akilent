"""The read-only Instagram reply diagnostic runs against a real conversation."""

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from apps.conversations.models import Conversation
from apps.instagram.tests.helpers import (
    dm_payload,
    make_account,
    make_instagram_account,
    signed_post,
)


class DiagnoseCommandTest(TestCase):
    def test_reports_account_customer_and_token_checks(self):
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event
        from apps.instagram.views import InstagramWebhookView

        account, _ = make_account()
        ig_account = make_instagram_account(account)
        with patch("apps.instagram.views.process_instagram_event"):
            InstagramWebhookView.as_view()(
                signed_post(dm_payload(ig_account.page_id, "igsid_d1", "hi"))
            )
        process_instagram_event(WebhookEventLog.objects.get().pk)
        conversation = Conversation.objects.get(channel="instagram")

        out = StringIO()
        with patch(
            "apps.instagram.management.commands.instagram_diagnose._get",
            return_value="HTTP 200 {}",
        ):
            call_command("instagram_diagnose", conversation.public_id, stdout=out)
        text = out.getvalue()
        self.assertIn("customer_igsid=igsid_d1", text)
        self.assertIn("<- used for replies", text)
        self.assertNotIn(ig_account.access_token, text)


class CheckCommentsCommandTest(TestCase):
    def test_runs_and_reports_rules_and_webhooks(self):
        from apps.instagram.models.trigger import CommentTrigger

        account, _ = make_account()
        ig_account = make_instagram_account(account)
        CommentTrigger.objects.create(
            account=account,
            name="Price",
            match_type=CommentTrigger.MatchType.KEYWORD,
            keywords="price",
            reply_template="Hi! Sending details by DM.",
        )
        out = StringIO()
        with patch(
            "apps.instagram.management.commands.instagram_check_comments.requests.get"
        ) as get:
            get.return_value.status_code = 200
            get.return_value.text = '{"data": []}'
            call_command("instagram_check_comments", stdout=out)
        text = out.getvalue()
        self.assertIn("Comment rules: 1 active / 1 total", text)
        self.assertIn("'Price'", text)
        self.assertNotIn(ig_account.access_token, text)

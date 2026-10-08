"""Comment webhooks fire comment-to-DM whichever payload shape Meta sends.

Meta's Instagram Login reference shows comments both as entry.changes[] and
with field/value set directly on the entry. Both must reach the trigger.
"""

from unittest.mock import patch

from django.test import TestCase

from apps.instagram.models.trigger import CommentTrigger
from apps.instagram.providers.base import SendResult
from apps.instagram.tests.helpers import (
    comment_payload,
    make_account,
    make_instagram_account,
    signed_post,
)


def _flat(payload: dict) -> dict:
    """The same comment with field/value on the entry instead of in changes[]."""
    entry = payload["entry"][0]
    change = entry.pop("changes")[0]
    entry.update(field=change["field"], value=change["value"])
    return payload


class CommentWebhookShapesTest(TestCase):
    def setUp(self):
        self.account, _ = make_account()
        self.ig_account = make_instagram_account(self.account)
        CommentTrigger.objects.create(
            account=self.account,
            name="Price",
            match_type=CommentTrigger.MatchType.KEYWORD,
            keywords="price",
            reply_template="Hi! Details in your DMs.",
        )

    def _deliver(self, payload):
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event
        from apps.instagram.views import InstagramWebhookView

        with patch("apps.instagram.views.process_instagram_event"):
            InstagramWebhookView.as_view()(signed_post(payload))
        event = WebhookEventLog.objects.order_by("-pk").first()
        with patch(
            "apps.instagram.providers.meta.MetaInstagramProvider.private_reply",
            return_value=SendResult(success=True, provider_message_id="mid.pr"),
        ) as private_reply:
            process_instagram_event(event.pk)
        return event, private_reply

    def test_changes_shape_sends_a_private_reply(self):
        event, private_reply = self._deliver(
            comment_payload(
                self.ig_account.page_id, "igsid_c1", "price?", comment_id="c1"
            )
        )
        self.assertEqual(event.event_type, "comment")
        private_reply.assert_called_once_with("c1", "Hi! Details in your DMs.")

    def test_flat_shape_sends_a_private_reply(self):
        event, private_reply = self._deliver(
            _flat(
                comment_payload(
                    self.ig_account.page_id, "igsid_c2", "price?", comment_id="c2"
                )
            )
        )
        self.assertEqual(event.event_type, "comment")
        private_reply.assert_called_once_with("c2", "Hi! Details in your DMs.")

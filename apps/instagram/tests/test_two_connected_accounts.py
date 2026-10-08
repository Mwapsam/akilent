"""Two professional accounts that both authorized Akilent, messaging each other.

Meta sends a webhook for each side. Only the copy addressed to the connected
business may reach its inbox; the other side's copy must never make the
business its own customer. Ids are from the 2026-10-08 production log.
"""

from unittest.mock import patch

from django.test import TestCase

from apps.conversations.models import Conversation
from apps.instagram.models.account import (
    TenantResolutionError,
    get_instagram_account_for_webhook,
)
from apps.instagram.models.contact import InstagramContact
from apps.instagram.tests.helpers import make_account, signed_post

BUSINESS = "17841444997993540"  # @mwapsam1, connected via Instagram Login
OTHER = "17841425872259405"  # the customer's own professional account
CUSTOMER_AS_SEEN_BY_BUSINESS = "1593832019208690"
BUSINESS_AS_SEEN_BY_OTHER = "1393602319600440"


def _dm(entry_id, sender, recipient, text, *, echo=False, mid):
    message = {"mid": mid, "text": text}
    if echo:
        message["is_echo"] = True
    return {
        "object": "instagram",
        "entry": [
            {
                "id": entry_id,
                "time": 1,
                "messaging": [
                    {
                        "sender": {"id": sender},
                        "recipient": {"id": recipient},
                        "timestamp": 1_790_000_000_000,
                        "message": message,
                    }
                ],
            }
        ],
    }


class TwoConnectedAccountsTest(TestCase):
    def setUp(self):
        from apps.instagram.models.account import InstagramBusinessAccount

        self.account, _ = make_account()
        # The stray page_id that sent the other account's webhooks here.
        self.iba = InstagramBusinessAccount.objects.create(
            account=self.account,
            instagram_business_account_id=BUSINESS,
            page_id=OTHER,
            access_token="IGAAtest",
            verify_token="v",
        )

    def _deliver(self, payload):
        from apps.instagram.models.webhook import WebhookEventLog
        from apps.instagram.tasks import process_instagram_event
        from apps.instagram.views import InstagramWebhookView

        with patch("apps.instagram.views.process_instagram_event"):
            InstagramWebhookView.as_view()(signed_post(payload))
        event = WebhookEventLog.objects.order_by("-pk").first()
        if event.instagram_account_id:
            process_instagram_event(event.pk)

    def test_page_id_on_an_instagram_login_account_is_not_matched(self):
        with self.assertRaises(TenantResolutionError):
            get_instagram_account_for_webhook(OTHER)
        self.assertEqual(get_instagram_account_for_webhook(BUSINESS), self.iba)

    def test_customer_message_lands_once_with_the_real_customer(self):
        # Customer writes "I'll take it": the business's copy, then the other side's echo.
        self._deliver(
            _dm(
                BUSINESS,
                CUSTOMER_AS_SEEN_BY_BUSINESS,
                BUSINESS,
                "I'll take it",
                mid="m1",
            )
        )
        self._deliver(
            _dm(
                OTHER,
                OTHER,
                BUSINESS_AS_SEEN_BY_OTHER,
                "I'll take it",
                echo=True,
                mid="m1b",
            )
        )
        # Business replies "hi" from the Instagram app: its echo, then the other side's copy.
        self._deliver(
            _dm(
                BUSINESS,
                BUSINESS,
                CUSTOMER_AS_SEEN_BY_BUSINESS,
                "hi",
                echo=True,
                mid="m2",
            )
        )
        self._deliver(_dm(OTHER, BUSINESS_AS_SEEN_BY_OTHER, OTHER, "hi", mid="m2b"))

        self.assertEqual(
            list(
                InstagramContact.objects.values_list("instagram_scoped_id", flat=True)
            ),
            [CUSTOMER_AS_SEEN_BY_BUSINESS],
        )
        conversation = Conversation.objects.get(channel="instagram")
        self.assertEqual(
            list(conversation.messages.order_by("pk").values_list("direction", "body")),
            [("inbound", "I'll take it"), ("outbound", "hi")],
        )

    def test_message_not_addressed_to_the_business_is_skipped(self):
        from apps.instagram.services.inbound import _process_dm_entry

        entry = _dm(BUSINESS, BUSINESS_AS_SEEN_BY_OTHER, OTHER, "hi", mid="m3")
        _process_dm_entry(self.iba, entry["entry"][0]["messaging"][0])
        self.assertFalse(InstagramContact.objects.exists())

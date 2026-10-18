"""A delivery that batches entries for several accounts is split per account."""

from unittest.mock import patch

from django.test import TestCase

from apps.instagram.models.webhook import WebhookEventLog
from apps.instagram.tests.helpers import (
    comment_payload,
    dm_payload,
    make_account,
    make_instagram_account,
    signed_post,
)
from apps.instagram.views import InstagramWebhookView


class BatchedWebhookTest(TestCase):
    def setUp(self):
        account_a, _ = make_account()
        account_b, _ = make_account()
        self.ig_a = make_instagram_account(account_a)
        self.ig_b = make_instagram_account(account_b)

    def _post(self, payload):
        with patch("apps.instagram.views.process_instagram_event"):
            return InstagramWebhookView.as_view()(signed_post(payload))

    def test_each_entry_is_stored_under_its_own_account(self):
        a = comment_payload(self.ig_a.instagram_business_account_id, "igsid_1", "hi")
        b = comment_payload(self.ig_b.instagram_business_account_id, "igsid_2", "yo")
        batched = {"object": "instagram", "entry": a["entry"] + b["entry"]}

        self.assertEqual(self._post(batched).status_code, 200)

        events = WebhookEventLog.objects.order_by("pk")
        self.assertEqual(events.count(), 2)
        self.assertEqual(
            [e.instagram_account_id for e in events], [self.ig_a.pk, self.ig_b.pk]
        )
        for event in events:
            self.assertEqual(len(event.raw_payload["entry"]), 1)

    def test_mixed_dm_and_comment_batch_keeps_both_kinds(self):
        dm = dm_payload(self.ig_a.instagram_business_account_id, "igsid_1", "hello")
        comment = comment_payload(
            self.ig_a.instagram_business_account_id, "igsid_1", "price?"
        )
        batched = {"object": "instagram", "entry": dm["entry"] + comment["entry"]}

        self._post(batched)

        kinds = set(WebhookEventLog.objects.values_list("event_type", flat=True))
        self.assertEqual(
            kinds,
            {WebhookEventLog.EventType.MESSAGE, WebhookEventLog.EventType.COMMENT},
        )

    def test_redelivery_of_a_batch_is_deduplicated_per_entry(self):
        a = comment_payload(self.ig_a.instagram_business_account_id, "igsid_1", "hi")
        b = comment_payload(self.ig_b.instagram_business_account_id, "igsid_2", "yo")
        batched = {"object": "instagram", "entry": a["entry"] + b["entry"]}

        self._post(batched)
        self._post(batched)

        self.assertEqual(WebhookEventLog.objects.count(), 2)

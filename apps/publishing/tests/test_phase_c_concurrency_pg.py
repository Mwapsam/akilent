"""Phase C: concurrent publication against a real PostgreSQL database.

SQLite ignores row locks, so these tests run only when the default database is
PostgreSQL and are skipped otherwise.
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.models import Account
from apps.publishing.models import ContentPost, ContentPostVariant
from apps.publishing.services import publish_variant

FB_OK = {
    "success": True,
    "publication_id": "fb_1",
    "publication_url": "https://facebook.com/fb_1",
    "error": "",
    "error_type": "",
    "error_at": None,
    "reconciling": False,
}


def _run(target, results, key):
    try:
        results[key] = target()
    except Exception as exc:
        results[key] = exc
    finally:
        connection.close()


@unittest.skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locks")
class ConcurrentPublicationTests(TransactionTestCase):
    def setUp(self):
        reviewer = User.objects.create_user("rev", "rev@example.com", "pass")
        account = Account.objects.create(company_name="Concurrency Co")
        self.post = ContentPost.objects.create(
            account=account,
            platforms=["facebook"],
            approval_required=True,
            approval_status="approved",
            approved_by=reviewer,
            approved_at=timezone.now(),
        )
        self.variant = ContentPostVariant.objects.create(
            post=self.post, platform="facebook", caption="Approved"
        )
        self.post.last_content_hash = self.post.compute_content_hash()
        self.post.save()

    def _publish_fresh(self):
        return publish_variant(ContentPostVariant.objects.get(pk=self.variant.pk))

    def test_two_concurrent_attempts_publish_exactly_once(self):
        provider_calls = []
        guard = threading.Lock()

        def provider(variant, post):
            with guard:
                provider_calls.append(variant.caption)
            return FB_OK

        results = {}
        with patch(
            "apps.publishing.services._publish_variant_facebook", side_effect=provider
        ):
            threads = [
                threading.Thread(target=_run, args=(self._publish_fresh, results, i))
                for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)

        assert len(provider_calls) == 1
        outcomes = sorted(r["success"] for r in results.values())
        assert outcomes == [False, True]

    def test_edit_during_provider_call_does_not_change_sent_payload(self):
        in_call = threading.Event()
        proceed = threading.Event()
        sent = {}

        def provider(variant, post):
            sent["caption"] = variant.caption
            in_call.set()
            proceed.wait(15)
            return FB_OK

        results = {}
        with patch(
            "apps.publishing.services._publish_variant_facebook", side_effect=provider
        ):
            worker = threading.Thread(
                target=_run, args=(self._publish_fresh, results, "pub")
            )
            worker.start()
            assert in_call.wait(15), "provider was never called"
            ContentPostVariant.objects.filter(pk=self.variant.pk).update(
                caption="Edited mid-call"
            )
            proceed.set()
            worker.join(30)

        assert sent["caption"] == "Approved"
        assert results["pub"]["success"] is True
        self.variant.refresh_from_db()
        assert self.variant.caption == "Edited mid-call"


@unittest.skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locks")
class ExhaustionEscalationPgTests(TransactionTestCase):
    def test_escalation_runs_on_postgresql_and_escalates_stale_instagram_variant(self):
        from datetime import timedelta

        from apps.publishing.tasks import escalate_exhausted_processing_variants

        account = Account.objects.create(company_name="Exhaustion Co")
        post = ContentPost.objects.create(account=account, platforms=["instagram"])
        variant = ContentPostVariant.objects.create(
            post=post,
            platform="instagram",
            status=ContentPostVariant.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(hours=72),
            metadata={"ig_container_id": "c1"},
        )

        result = escalate_exhausted_processing_variants(max_age_hours=48)

        variant.refresh_from_db()
        assert result["escalated"] == 1
        assert variant.status == ContentPostVariant.Status.RECONCILING

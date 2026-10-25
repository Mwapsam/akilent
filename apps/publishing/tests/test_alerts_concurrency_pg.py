"""Two alert sweeps racing for the same channel, against a real PostgreSQL database.

The claim is recorded under a row lock before any email goes out, so exactly one sweep
sends. SQLite ignores row locks, so this runs only on PostgreSQL.
"""

from __future__ import annotations

import threading
import unittest
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.publishing import alerts
from apps.publishing.models import ContentPost, ContentPostVariant

pytestmark = pytest.mark.serial


@unittest.skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locks")
class ConcurrentAlertSweepTests(TransactionTestCase):
    def setUp(self):
        account = Account.objects.create(company_name="Race Co")
        owner = User.objects.create_user("race", "race@example.com", "pass")
        Membership.objects.create(
            user=owner, account=account, role=Membership.Role.OWNER
        )
        post = ContentPost.objects.create(
            account=account, platforms=["facebook"], media_type="text", caption="x"
        )
        self.variant = ContentPostVariant.objects.create(
            post=post,
            platform="facebook",
            status=ContentPostVariant.Status.FAILED,
            last_error="Bad media",
            last_error_type=ContentPostVariant.ErrorType.VALIDATION,
            last_error_at=timezone.now() - timedelta(minutes=20),
        )

    def test_two_sweeps_at_once_send_one_email(self):
        sent = []
        lock = threading.Lock()
        start = threading.Barrier(2)

        def fake_send(**kw):
            with lock:
                sent.append(kw["to_email"])

        def sweep():
            try:
                start.wait()
                alerts.send_due_alerts()
            finally:
                connection.close()

        with patch("apps.email.services.send.send_system_email", side_effect=fake_send):
            threads = [threading.Thread(target=sweep) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)

        assert sent == ["race@example.com"]
        self.variant.refresh_from_db()
        assert list(self.variant.metadata[alerts.ALERTS_KEY]) == ["failed"]

"""PostgreSQL-only: the TOTP device row lock serialises concurrent guesses.

With select_for_update(), the first request holds the row, records its failure,
and commits. Every request queued behind it then reads the new failure timestamp
and is refused by the lockout before it reaches token verification. So exactly
one verify_token() call happens, however many threads race.

Skipped on SQLite, which has no row locks. Run with USE_SQLITE=0 against a
PostgreSQL server.
"""

import threading
import time
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import TransactionTestCase
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.views import _check_second_factor

pytestmark = pytest.mark.serial


class PostgresRowLockTest(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("row lock behaviour needs PostgreSQL")
        self.user = User.objects.create_user(
            username="lock@example.com",
            email="lock@example.com",
            password="pw-12345678",
        )
        self.device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )

    def test_concurrent_wrong_codes_reach_verification_once(self):
        original = TOTPDevice.verify_token
        verify_calls = []

        def counting_verify(device, token):
            verify_calls.append(token)
            # Hold the first attempt open long enough for the others to arrive.
            # Without the row lock they would read the stale failure count and
            # verify too; with it they block and are refused by the lockout.
            time.sleep(0.3)
            return original(device, token)

        threads_n = 8
        barrier = threading.Barrier(threads_n)
        errors = []

        def attempt():
            try:
                barrier.wait()
                _check_second_factor(self.user, self.device, "000000")
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        with mock.patch.object(TOTPDevice, "verify_token", counting_verify):
            threads = [threading.Thread(target=attempt) for _ in range(threads_n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(verify_calls), 1)
        self.device.refresh_from_db()
        self.assertEqual(self.device.throttling_failure_count, 1)

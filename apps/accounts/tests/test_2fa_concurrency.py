"""PR-2 pre-PR-3 checks: concurrent redemption, session state, cross-factor lockout.

Tests run on SQLite, where select_for_update() is a no-op. The recovery-code
guarantee therefore rests on one conditional UPDATE, which is atomic on both
SQLite and PostgreSQL. The device row lock is exercised on PostgreSQL only.
"""

import threading

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import recovery
from apps.accounts.tests.test_2fa import _current_token

pytestmark = pytest.mark.serial

PASSWORD = "pw-12345678"


class ConcurrentRedemptionTest(TransactionTestCase):
    def test_exactly_one_thread_wins_a_recovery_code(self):
        user = User.objects.create_user(
            username="race@example.com", email="race@example.com", password=PASSWORD
        )
        code = recovery.generate_codes(user)[0]
        results, errors = [], []
        barrier = threading.Barrier(8)

        def attempt():
            try:
                barrier.wait()
                results.append(recovery.redeem(user, code))
            except Exception as exc:  # surfaced below; a DB error is a failure
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(results.count(True), 1)


class SessionStateAndCrossFactorLockoutTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="sess@example.com", email="sess@example.com", password=PASSWORD
        )
        self.device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )
        self.client = Client()

    def _second_step(self):
        self.client.post(
            reverse("login"),
            {"username": "sess@example.com", "password": PASSWORD},
        )

    def test_totp_sign_in_marks_session_otp_verified(self):
        self._second_step()

        self.client.post(reverse("2fa-verify"), {"token": _current_token(self.device)})

        self.assertEqual(
            self.client.session[DEVICE_ID_SESSION_KEY], self.device.persistent_id
        )

    def test_failed_recovery_attempts_lock_out_totp_too(self):
        recovery.generate_codes(self.user)
        self._second_step()
        # The first wrong recovery code opens a delay. Attempts inside that
        # delay are refused without being counted, so the counter stays at 1.
        self.client.post(reverse("2fa-verify"), {"token": "AAAA-BBBB-CCCC-DDDD"})
        self.client.post(reverse("2fa-verify"), {"token": _current_token(self.device)})

        self.assertNotIn(DEVICE_ID_SESSION_KEY, self.client.session)
        self.device.refresh_from_db()
        self.assertEqual(self.device.throttling_failure_count, 1)

    def test_lockout_expires_so_the_owner_is_not_locked_out_for_good(self):
        from datetime import timedelta

        from django.utils import timezone

        self.device.throttling_failure_count = 3
        self.device.throttling_failure_timestamp = timezone.now() - timedelta(hours=1)
        self.device.save()
        self._second_step()

        self.client.post(reverse("2fa-verify"), {"token": _current_token(self.device)})

        self.assertEqual(
            self.client.session[DEVICE_ID_SESSION_KEY], self.device.persistent_id
        )

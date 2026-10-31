"""Sign-in stays fast: email codes are queued, and each attempt hashes the password once."""

import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import authenticate, hashers
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.utils import timezone

from apps.accounts import email_mfa
from apps.accounts.models import EmailLoginCode
from apps.accounts.tasks import send_email_code

PASSWORD = "pw-12345678"
DELAY = "apps.accounts.tasks.send_email_code.delay"


def _user(email="speed@example.com"):
    return User.objects.create_user(username=email, email=email, password=PASSWORD)


class EmailCodeIsQueuedTest(TestCase):
    def setUp(self):
        self.user = _user()
        mail.outbox = []

    def test_send_code_queues_the_row_and_sends_nothing_inline(self):
        with mock.patch(DELAY) as delay:
            result = email_mfa.send_code(self.user, "login")

        row = EmailLoginCode.objects.get(user=self.user)
        self.assertEqual(result, email_mfa.SENT)
        delay.assert_called_once_with(row.pk)
        self.assertEqual(row.code_hash, email_mfa.PENDING_HASH)
        self.assertEqual(mail.outbox, [])

    def test_a_queued_row_matches_no_code(self):
        with mock.patch(DELAY):
            email_mfa.send_code(self.user, "login")

        self.assertFalse(email_mfa.check_code(self.user, "login", "pending"))
        self.assertFalse(email_mfa.check_code(self.user, "login", "000000"))

    def test_the_task_emails_a_code_that_then_works(self):
        with mock.patch(DELAY) as delay:
            email_mfa.send_code(self.user, "login")
        send_email_code(delay.call_args.args[0])

        code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
        self.assertTrue(email_mfa.check_code(self.user, "login", code))

    def test_a_broker_outage_reports_unavailable_and_keeps_no_row(self):
        with mock.patch(DELAY, side_effect=ConnectionError("broker down")):
            result = email_mfa.send_code(self.user, "login")

        self.assertEqual(result, email_mfa.UNAVAILABLE)
        self.assertFalse(EmailLoginCode.objects.filter(user=self.user).exists())

    def test_the_task_skips_a_row_that_was_replaced_or_expired(self):
        with mock.patch(DELAY) as delay:
            email_mfa.send_code(self.user, "login")
        code_id = delay.call_args.args[0]

        EmailLoginCode.objects.filter(pk=code_id).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        send_email_code(code_id)

        self.assertEqual(mail.outbox, [])


class OneHashPerSignInTest(TestCase):
    def setUp(self):
        self.user = _user("hash@example.com")

    def _hashes(self, username, password):
        with (
            mock.patch(
                "django.contrib.auth.base_user.check_password",
                wraps=hashers.check_password,
            ) as check,
            mock.patch(
                "django.contrib.auth.base_user.make_password",
                wraps=hashers.make_password,
            ) as make,
        ):
            result = authenticate(username=username, password=password)
        return result, check.call_count + make.call_count

    def test_correct_password(self):
        user, hashes = self._hashes("hash@example.com", PASSWORD)
        self.assertEqual(user, self.user)
        self.assertEqual(hashes, 1)

    def test_wrong_password_for_a_known_email(self):
        user, hashes = self._hashes("HASH@example.com", "wrong-password")
        self.assertIsNone(user)
        self.assertEqual(hashes, 1)

    def test_unknown_email(self):
        user, hashes = self._hashes("nobody@example.com", "wrong-password")
        self.assertIsNone(user)
        self.assertEqual(hashes, 1)

    def test_username_sign_in_still_works(self):
        admin = User.objects.create_user(
            username="opsadmin", email="ops@example.com", password=PASSWORD
        )
        user, hashes = self._hashes("opsadmin", PASSWORD)
        self.assertEqual(user, admin)
        self.assertEqual(hashes, 1)

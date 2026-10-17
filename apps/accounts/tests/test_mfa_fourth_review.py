"""Regression tests for the fourth review: failed sends, duplicate passkeys, reset counts."""

from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts import email_mfa, passkeys
from apps.accounts.models import EmailLoginCode, PasskeyCredential

PASSWORD = "pw-12345678"


def _user(email):
    return User.objects.create_user(username=email, email=email, password=PASSWORD)


class FailedSendDoesNotStartACooldownTest(TestCase):
    def test_a_retry_right_after_a_failed_send_actually_sends(self):
        user = _user("failsend@example.com")
        mail.outbox = []

        with mock.patch(
            "apps.email.services.send.send_system_email",
            side_effect=RuntimeError("down"),
        ):
            first = email_mfa.send_code(user, "login")
        second = email_mfa.send_code(user, "login")

        self.assertEqual(first, email_mfa.UNAVAILABLE)
        self.assertEqual(second, email_mfa.SENT)
        self.assertEqual(
            EmailLoginCode.objects.filter(user=user, used_at__isnull=True).count(), 1
        )


class DuplicatePasskeyIsRefusedTest(TestCase):
    def test_an_already_registered_authenticator_returns_none(self):
        user = _user("dupkey@example.com")
        PasskeyCredential.objects.create(
            user=user,
            name="Old",
            credential_id=b"same-key",
            public_key=b"p",
            confirmed=True,
        )
        verified = mock.Mock(
            credential_id=b"same-key", credential_public_key=b"p", sign_count=0
        )
        request = mock.Mock()
        request.session = {}
        request.get_host.return_value = "testserver"
        request.scheme = "http"
        request.session[passkeys.REG_KEY] = {
            "challenge": "AAAA",
            "at": timezone.now().isoformat(),
        }

        with mock.patch(
            "apps.accounts.passkeys.verify_registration_response", return_value=verified
        ):
            result = passkeys.finish_registration(request, user, {"id": "x"}, "Again")

        self.assertIsNone(result)
        self.assertEqual(PasskeyCredential.objects.filter(user=user).count(), 1)


class ResetDryRunCountsPasskeysTest(TestCase):
    def test_dry_run_reports_passkeys_and_email_codes(self):
        target = _user("countkeys@example.com")
        operator = User.objects.create_user(
            username="op-count@example.com",
            email="op-count@example.com",
            password=PASSWORD,
        )
        operator.is_staff = True
        operator.save()
        PasskeyCredential.objects.create(
            user=target,
            name="Key",
            credential_id=b"cnt",
            public_key=b"p",
            confirmed=True,
        )
        out = StringIO()

        call_command(
            "reset_user_mfa",
            "--email",
            target.email,
            "--ticket",
            "SUP-303",
            "--approved-by",
            operator.email,
            stdout=out,
        )

        self.assertIn("1 passkey(s)", out.getvalue())
        self.assertIn("Dry run", out.getvalue())

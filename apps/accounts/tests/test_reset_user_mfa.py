"""Operator MFA reset: two-person, ticketed, audited, dry-run by default."""

from io import StringIO

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import recovery
from apps.accounts.models import RecoveryCode
from apps.core.models import PlatformAuditLog


def _user(email, staff=False):
    user = User.objects.create_user(username=email, email=email, password="pw-12345678")
    user.is_staff = staff
    user.save()
    return user


class ResetUserMfaTest(TestCase):
    def setUp(self):
        self.target = _user("locked@example.com")
        self.approver = _user("ops@example.com", staff=True)
        TOTPDevice.objects.create(user=self.target, name="default", confirmed=True)
        recovery.generate_codes(self.target)

    def _run(
        self,
        *extra,
        email="locked@example.com",
        approver="ops@example.com",
        ticket="SUP-101",
    ):
        out = StringIO()
        call_command(
            "reset_user_mfa",
            "--email",
            email,
            "--ticket",
            ticket,
            "--approved-by",
            approver,
            *extra,
            stdout=out,
        )
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        out = self._run()

        self.assertIn("Dry run", out)
        self.assertTrue(TOTPDevice.objects.filter(user=self.target).exists())
        self.assertEqual(RecoveryCode.objects.filter(user=self.target).count(), 10)
        self.assertEqual(len(mail.outbox), 0)

    def test_confirmed_reset_removes_factors_and_writes_audit(self):
        self._run("--confirm")

        self.assertFalse(TOTPDevice.objects.filter(user=self.target).exists())
        self.assertFalse(RecoveryCode.objects.filter(user=self.target).exists())
        entry = PlatformAuditLog.objects.get(action="auth.mfa_operator_reset")
        self.assertEqual(entry.metadata["ticket"], "SUP-101")
        self.assertEqual(entry.metadata["approved_by"], "ops@example.com")

    def test_confirmed_reset_ends_the_users_sessions(self):
        client = Client()
        client.force_login(self.target)
        self.assertTrue(Session.objects.exists())

        self._run("--confirm")

        self.assertFalse(client.get("/").wsgi_request.user.is_authenticated)

    def test_confirmed_reset_emails_the_user_with_the_ticket(self):
        self._run("--confirm")

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["locked@example.com"])
        self.assertIn("SUP-101", mail.outbox[0].body)

    def test_approver_must_be_platform_staff(self):
        _user("plain@example.com", staff=False)

        with self.assertRaisesMessage(CommandError, "platform staff"):
            self._run("--confirm", approver="plain@example.com")
        self.assertTrue(TOTPDevice.objects.filter(user=self.target).exists())

    def test_approver_cannot_reset_their_own_account(self):
        with self.assertRaisesMessage(CommandError, "cannot be the user"):
            self._run("--confirm", email="ops@example.com", approver="ops@example.com")

    def test_ticket_is_required(self):
        with self.assertRaisesMessage(CommandError, "ticket"):
            self._run("--confirm", ticket="   ")
        self.assertTrue(TOTPDevice.objects.filter(user=self.target).exists())

"""Recovery-code policy, owner notifications, and session fixation.

Policy: recovery codes are accepted by sign-in (on their own page), by turning
two-factor off, and by regenerating codes. Each code works once, whichever form
redeems it. Owners are emailed after a regeneration or a turn-off commits.
"""

from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user
from django.contrib.sessions.backends.db import SessionStore
from django.core import mail
from django.db import transaction
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import recovery
from apps.accounts.models import RecoveryCode
from apps.accounts.security_notices import (
    RECOVERY_REGENERATED,
    TWO_FACTOR_DISABLED,
    notify_after_commit,
)
from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_2fa_hardening import _enrolled, _user
from apps.core.models import PlatformAuditLog as AuditLog

PASSWORD = "pw-12345678"


class RecoveryPolicyTest(TestCase):
    def setUp(self):
        self.user = _user("policy@example.com")
        self.device = _enrolled(self.user)
        self.codes = recovery.generate_codes(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def _disable(self, token):
        return self.client.post(
            reverse("2fa-setup"),
            {"action": "disable", "password": PASSWORD, "token": token},
        )

    def test_a_valid_recovery_code_turns_two_factor_off(self):
        self._disable(self.codes[0])

        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())
        self.assertEqual(RecoveryCode.objects.filter(user=self.user).count(), 0)

    def test_an_invalid_recovery_code_does_not_turn_two_factor_off(self):
        self._disable("AAAA-BBBB-CCCC-DDDD")

        self.assertTrue(TOTPDevice.objects.filter(user=self.user).exists())
        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT)

    def test_a_consumed_recovery_code_cannot_turn_two_factor_off(self):
        self.assertTrue(recovery.redeem(self.user, self.codes[0]))

        self._disable(self.codes[0])

        self.assertTrue(TOTPDevice.objects.filter(user=self.user).exists())

    def test_a_consumed_recovery_code_cannot_regenerate(self):
        self.assertTrue(recovery.redeem(self.user, self.codes[0]))

        self.client.post(
            reverse("recovery-codes-regenerate"),
            {"password": PASSWORD, "token": self.codes[0]},
        )

        self.assertFalse(self.client.session.get("recovery_display") is not None)
        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT - 1)

    def test_a_valid_recovery_code_can_authorise_regeneration(self):
        self.client.post(
            reverse("recovery-codes-regenerate"),
            {"password": PASSWORD, "token": self.codes[1]},
        )

        self.assertFalse(recovery.redeem(self.user, self.codes[2]))
        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT)

    def test_a_wrong_password_does_not_spend_a_recovery_code(self):
        self.client.post(
            reverse("recovery-codes-regenerate"),
            {"password": "wrong", "token": self.codes[0]},
        )

        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT)


class OwnerNotificationTest(TestCase):
    def setUp(self):
        self.user = _user("notify@example.com")
        self.device = _enrolled(self.user)
        self.codes = recovery.generate_codes(self.user)
        self.client = Client()
        self.client.force_login(self.user)
        mail.outbox = []

    def test_regeneration_emails_the_owner_without_any_code(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("recovery-codes-regenerate"),
                {"password": PASSWORD, "token": _current_token(self.device)},
            )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("recovery codes were replaced", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        shown = self.client.get(reverse("settings-recovery-codes")).context["codes"]
        self.assertEqual(len(shown), recovery.CODE_COUNT)
        for code in shown:
            self.assertNotIn(code, mail.outbox[0].body)
            self.assertNotIn(code.replace("-", ""), mail.outbox[0].body)

    def test_turning_off_two_factor_emails_the_owner(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("2fa-setup"),
                {
                    "action": "disable",
                    "password": PASSWORD,
                    "token": _current_token(self.device),
                },
            )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("turned off", mail.outbox[0].subject)

    def test_a_rolled_back_change_sends_no_email(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    notify_after_commit(self.user, TWO_FACTOR_DISABLED)
                    raise RuntimeError("change rolled back")
            except RuntimeError:
                pass

        self.assertEqual(mail.outbox, [])

    def test_a_delivery_failure_keeps_the_change_and_is_audited(self):
        with (
            mock.patch(
                "apps.email.services.send.send_system_email",
                side_effect=RuntimeError("provider down"),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            resp = self.client.post(
                reverse("recovery-codes-regenerate"),
                {"password": PASSWORD, "token": _current_token(self.device)},
                follow=True,
            )

        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT)
        self.assertNotContains(resp, "email was sent")
        self.assertTrue(
            AuditLog.objects.filter(action="auth.security_notice_failed").exists()
        )

    def test_no_email_is_sent_when_the_owner_has_no_address(self):
        self.user.email = ""
        self.user.save()
        with (
            mock.patch("apps.email.services.send.send_system_email") as send,
            self.captureOnCommitCallbacks(execute=True),
        ):
            notify_after_commit(self.user, RECOVERY_REGENERATED)

        send.assert_not_called()


class SessionFixationTest(TestCase):
    def setUp(self):
        self.user = _user("fixation@example.com")
        self.device = _enrolled(self.user)

    def test_login_rotates_the_session_key_and_keeps_unrelated_data(self):
        client = Client()
        session = client.session  # creates the pre-login session and its cookie
        session["language"] = "fr"
        session.save()
        before = client.cookies[settings.SESSION_COOKIE_NAME].value

        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )
        client.post(reverse("2fa-verify"), {"token": _current_token(self.device)})

        after = client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertNotEqual(before, after)
        self.assertEqual(client.session.get("language"), "fr")
        self.assertTrue(get_user(client).is_authenticated)

    def test_a_planted_session_key_is_not_authenticated_after_login(self):
        planted = SessionStore()
        planted.create()
        client = Client()
        client.cookies[settings.SESSION_COOKIE_NAME] = planted.session_key

        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )
        client.post(reverse("2fa-verify"), {"token": _current_token(self.device)})

        self.assertNotEqual(
            client.cookies[settings.SESSION_COOKIE_NAME].value, planted.session_key
        )
        self.assertFalse(
            SessionStore(session_key=planted.session_key).exists(planted.session_key)
        )

    def test_sign_in_state_planted_before_login_is_cleared_at_the_password_step(self):
        from apps.accounts.views import STRONG_VERIFIED_KEY

        client = Client()
        session = client.session
        session[STRONG_VERIFIED_KEY] = {
            "at": timezone.now().isoformat(),
            "user": self.user.pk,
        }
        session["recovery_display"] = "stale"
        session.save()

        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )

        self.assertNotIn(STRONG_VERIFIED_KEY, client.session)
        self.assertNotIn("recovery_display", client.session)

    def test_the_password_step_alone_does_not_authenticate(self):
        client = Client()
        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )

        resp = client.get(reverse("settings-security"))

        self.assertFalse(get_user(client).is_authenticated)
        self.assertRedirects(
            resp,
            f"{reverse('login')}?next={reverse('settings-security')}",
            fetch_redirect_response=False,
        )

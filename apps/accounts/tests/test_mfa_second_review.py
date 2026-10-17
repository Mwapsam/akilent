"""Regression tests for the second review: step-up loops, enrolment bypass, channels, limits."""

from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import EmailMfaEnrolment, PasskeyCredential
from apps.accounts.tests.test_email_mfa import _last_code

PASSWORD = "pw-12345678"


def _user(email, staff=False):
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_staff = staff
    user.save()
    return user


@override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
class BadActionsDoNotTouchTheThrottleTest(TestCase):
    def setUp(self):
        self.user = _user("badaction@example.com")
        self.device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )
        self.client = Client()
        self.client.post(
            reverse("login"),
            {"username": "badaction@example.com", "password": PASSWORD},
        )

    def test_unknown_action_does_not_count_as_a_failed_code(self):
        for _ in range(3):
            self.client.post(reverse("2fa-verify"), {"action": "something-else"})

        self.device.refresh_from_db()
        self.assertEqual(self.device.throttling_failure_count, 0)

    def test_email_action_without_email_codes_does_not_count_as_a_failed_code(self):
        for _ in range(3):
            self.client.post(reverse("2fa-verify"), {"action": "send_email"})

        self.device.refresh_from_db()
        self.assertEqual(self.device.throttling_failure_count, 0)


class ChannelIsClearedOnANewLoginTest(TestCase):
    def test_a_new_password_login_does_not_inherit_the_email_channel(self):
        user = _user("channelreset@example.com")
        EmailMfaEnrolment.objects.create(user=user)
        TOTPDevice.objects.create(user=user, name="default", confirmed=True)
        client = Client()
        client.post(
            reverse("login"),
            {"username": "channelreset@example.com", "password": PASSWORD},
        )
        client.post(reverse("2fa-verify"), {"action": "send_email"})

        client.logout()
        client.post(
            reverse("login"),
            {"username": "channelreset@example.com", "password": PASSWORD},
        )

        self.assertNotIn("_2fa_channel", client.session)


class SendFailureIsSaidTest(TestCase):
    def test_a_failed_send_tells_the_user_it_failed(self):
        user = _user("sendfail@example.com")
        EmailMfaEnrolment.objects.create(user=user)
        client = Client()
        client.post(
            reverse("login"), {"username": "sendfail@example.com", "password": PASSWORD}
        )

        with mock.patch(
            "apps.email.services.send.send_system_email",
            side_effect=RuntimeError("down"),
        ):
            resp = client.post(
                reverse("2fa-verify"), {"action": "send_email"}, follow=True
            )

        self.assertContains(resp, "couldn&#x27;t send a code")


class PasskeyReauthSurvivesAFailedTryTest(TestCase):
    def test_a_failed_browser_step_does_not_use_up_the_reauth(self):
        from apps.accounts.tests.test_passkeys import _fake_registration

        user = _user("reauthkeep@example.com")
        client = Client()
        client.force_login(user)
        client.post(reverse("passkey-reauth-send"))
        options = client.post(
            reverse("passkey-register-options"),
            data='{"method": "email", "token": "%s"}' % _last_code(),
            content_type="application/json",
        )
        self.assertEqual(options.status_code, 200)

        with mock.patch(
            "apps.accounts.passkeys.verify_registration_response",
            side_effect=ValueError("cancelled"),
        ):
            failed = client.post(
                reverse("passkey-register-finish"),
                data='{"credential": {}, "name": "x"}',
                content_type="application/json",
            )
        # A real retry asks for fresh options first; no new code is needed.
        again = client.post(
            reverse("passkey-register-options"),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(again.status_code, 200)
        with mock.patch(
            "apps.accounts.passkeys.verify_registration_response",
            return_value=_fake_registration(b"retry"),
        ):
            retried = client.post(
                reverse("passkey-register-finish"),
                data='{"credential": {}, "name": "x"}',
                content_type="application/json",
            )

        self.assertEqual(failed.status_code, 400)
        self.assertEqual(retried.status_code, 200)
        self.assertTrue(PasskeyCredential.objects.filter(user=user, name="x").exists())


@override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
class PasskeyUserNoEmailTest(TestCase):
    """A required user with a passkey gets no email channel at all."""

    def setUp(self):
        self.staff = _user("pk-noemail@example.com", staff=True)
        PasskeyCredential.objects.create(
            user=self.staff,
            name="Key",
            credential_id=b"pn",
            public_key=b"p",
            confirmed=True,
        )
        EmailMfaEnrolment.objects.create(user=self.staff)
        self.client = Client()
        mail.outbox = []

    def test_email_is_not_offered_to_a_passkey_user(self):
        self.client.post(
            reverse("login"),
            {"username": "pk-noemail@example.com", "password": PASSWORD},
        )
        self.client.post(reverse("2fa-verify"), {"action": "send_email"})

        self.assertEqual(mail.outbox, [])

    def test_a_passkey_only_required_user_cannot_reach_enrolment_unverified(self):
        client = Client()
        client.force_login(self.staff)

        resp = client.get(reverse("2fa-setup"))

        self.assertRedirects(resp, reverse("2fa-verify"), fetch_redirect_response=False)

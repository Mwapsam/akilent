"""Regression tests for the MFA review findings: limits, throttles, reset and reachability."""

from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import email_mfa
from apps.accounts.models import EmailLoginCode, EmailMfaEnrolment, PasskeyCredential
from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_email_mfa import _last_code

PASSWORD = "pw-12345678"


def _user(email, staff=False, password=PASSWORD):
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_staff = staff
    user.save()
    return user


class EmailAccountLimitsTest(TestCase):
    def setUp(self):
        self.user = _user("limits@example.com")
        mail.outbox = []

    def test_no_more_than_five_codes_are_sent_per_hour(self):
        sent = []
        for _ in range(7):
            sent.append(email_mfa.send_code(self.user, "login"))
            # Move past the per-code cooldown so only the hourly cap applies.
            EmailLoginCode.objects.update(
                created_at=timezone.now() - timedelta(minutes=2)
            )

        self.assertEqual(sent.count(email_mfa.SENT), 5)
        self.assertEqual(len(mail.outbox), 5)

    def test_guesses_across_codes_are_capped_per_hour(self):
        for _ in range(3):
            email_mfa.send_code(self.user, "login")
            EmailLoginCode.objects.update(
                created_at=timezone.now() - timedelta(minutes=2)
            )
        # Spend the per-account guess budget across several codes.
        for row in EmailLoginCode.objects.all():
            row.attempts = 4
            row.save(update_fields=["attempts"])

        real = _last_code()
        self.assertFalse(email_mfa.check_code(self.user, "login", real))


@override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
class RequiredUserReachabilityTest(TestCase):
    def test_required_user_without_a_factor_can_reach_enrolment_pages(self):
        staff = _user("nofactor@example.com", staff=True)
        client = Client()
        client.force_login(staff)

        passkeys_page = client.get(reverse("settings-passkeys"))
        email_page = client.get(reverse("settings-email-mfa"))

        self.assertEqual(passkeys_page.status_code, 200)
        self.assertEqual(email_page.status_code, 200)

    def test_required_user_with_a_passkey_can_be_sent_to_the_prompt(self):
        staff = _user("haskey@example.com", staff=True)
        PasskeyCredential.objects.create(
            user=staff, name="Key", credential_id=b"k", public_key=b"p", confirmed=True
        )
        client = Client()
        client.force_login(staff)

        resp = client.get(reverse("settings-security"))

        self.assertRedirects(resp, reverse("2fa-verify"), fetch_redirect_response=False)


@override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
class PasskeyPageNeedsFreshFactorTest(TestCase):
    def test_required_user_with_passkey_must_verify_before_the_passkey_pages(self):
        from apps.accounts.models import PasskeyCredential

        staff = _user("pkpage@example.com", staff=True)
        PasskeyCredential.objects.create(
            user=staff, name="Key", credential_id=b"kp", public_key=b"p", confirmed=True
        )
        client = Client()
        client.force_login(staff)

        resp = client.get(reverse("settings-passkeys"))

        self.assertRedirects(resp, reverse("2fa-verify"), fetch_redirect_response=False)


class OperatorResetRemovesEveryFactorTest(TestCase):
    def test_reset_removes_passkeys_and_email_codes_too(self):
        target = _user("everything@example.com")
        operator = _user("ops2@example.com", staff=True)
        TOTPDevice.objects.create(user=target, name="default", confirmed=True)
        PasskeyCredential.objects.create(
            user=target,
            name="Key",
            credential_id=b"k2",
            public_key=b"p",
            confirmed=True,
        )
        EmailMfaEnrolment.objects.create(user=target)

        call_command(
            "reset_user_mfa",
            "--email",
            target.email,
            "--ticket",
            "SUP-202",
            "--approved-by",
            operator.email,
            "--confirm",
            stdout=StringIO(),
        )

        self.assertFalse(TOTPDevice.objects.filter(user=target).exists())
        self.assertFalse(PasskeyCredential.objects.filter(user=target).exists())
        self.assertFalse(EmailMfaEnrolment.objects.filter(user=target).exists())


class TotpAccountHasNoEmailChannelTest(TestCase):
    """Email codes never weaken an account that already has an authenticator app."""

    def setUp(self):
        self.user = _user("totponly@example.com")
        self.device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )
        self.client = Client()
        mail.outbox = []

    def test_email_is_not_offered_to_an_app_account(self):
        self.client.post(
            reverse("login"), {"username": "totponly@example.com", "password": PASSWORD}
        )

        resp = self.client.get(reverse("2fa-verify"))

        self.assertNotContains(resp, "Email me a code instead")

    def test_an_emailed_send_for_an_app_account_sends_nothing(self):
        self.client.post(
            reverse("login"), {"username": "totponly@example.com", "password": PASSWORD}
        )

        self.client.post(reverse("2fa-verify"), {"action": "send_email"})

        self.assertEqual(mail.outbox, [])

    def test_an_app_account_cannot_enrol_email_codes(self):
        self.client.force_login(self.user)

        self.client.post(
            reverse("settings-email-mfa"),
            {"action": "send_enrol"},
        )

        self.assertEqual(mail.outbox, [])
        self.assertFalse(EmailMfaEnrolment.objects.filter(user=self.user).exists())

    def test_the_authenticator_app_still_signs_in(self):
        self.client.post(
            reverse("login"), {"username": "totponly@example.com", "password": PASSWORD}
        )

        self.client.post(reverse("2fa-verify"), {"token": _current_token(self.device)})

        self.assertIn(DEVICE_ID_SESSION_KEY, self.client.session)


class EmailOnlyPromptTest(TestCase):
    def setUp(self):
        self.user = _user("emailonly@example.com")
        EmailMfaEnrolment.objects.create(user=self.user)
        self.client = Client()
        mail.outbox = []

    def test_an_email_only_account_gets_a_send_button_on_the_prompt(self):
        self.client.post(
            reverse("login"),
            {"username": "emailonly@example.com", "password": PASSWORD},
        )

        resp = self.client.get(reverse("2fa-verify"))

        self.assertContains(resp, "Email me a new code")

    def test_sending_then_signing_in_with_the_emailed_code_works(self):
        self.client.post(
            reverse("login"),
            {"username": "emailonly@example.com", "password": PASSWORD},
        )
        self.client.post(reverse("2fa-verify"), {"action": "send_email"})

        self.client.post(reverse("2fa-verify"), {"token": _last_code()})

        self.assertIn("_auth_user_id", self.client.session)

"""Email sign-in codes: lifecycle, sign-in flow, enrolment, and the enforcement rule."""

import re
from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY

from apps.accounts import email_mfa
from apps.accounts.models import EmailLoginCode, EmailMfaEnrolment

PASSWORD = "pw-12345678"


def _user(email="mail@example.com", staff=False):
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_staff = staff
    user.save()
    return user


def _last_code():
    return re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)


class CodeLifecycleTest(TestCase):
    def setUp(self):
        self.user = _user()
        mail.outbox = []

    def test_code_is_emailed_and_only_its_hash_is_stored(self):
        email_mfa.send_code(self.user, "login")

        code = _last_code()
        row = EmailLoginCode.objects.get(user=self.user)
        self.assertNotEqual(row.code_hash, code)
        self.assertEqual(len(row.code_hash), 64)

    def test_correct_code_passes_once(self):
        email_mfa.send_code(self.user, "login")
        code = _last_code()

        self.assertTrue(email_mfa.check_code(self.user, "login", code))
        self.assertFalse(email_mfa.check_code(self.user, "login", code))

    def test_five_wrong_guesses_lock_the_code(self):
        email_mfa.send_code(self.user, "login")
        code = _last_code()
        wrong = "000000" if code != "000000" else "111111"

        for _ in range(5):
            email_mfa.check_code(self.user, "login", wrong)

        self.assertFalse(email_mfa.check_code(self.user, "login", code))

    def test_expired_code_is_refused(self):
        email_mfa.send_code(self.user, "login")
        code = _last_code()
        EmailLoginCode.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        self.assertFalse(email_mfa.check_code(self.user, "login", code))

    def test_a_new_code_invalidates_the_previous_one(self):
        email_mfa.send_code(self.user, "login")
        first = _last_code()
        EmailLoginCode.objects.update(created_at=timezone.now() - timedelta(minutes=2))
        email_mfa.send_code(self.user, "login")

        self.assertFalse(email_mfa.check_code(self.user, "login", first))

    def test_requests_inside_the_cooldown_send_nothing(self):
        self.assertEqual(email_mfa.send_code(self.user, "login"), email_mfa.SENT)
        self.assertEqual(email_mfa.send_code(self.user, "login"), email_mfa.RECENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_code_for_one_purpose_does_not_work_for_another(self):
        email_mfa.send_code(self.user, "enrol")
        code = _last_code()

        self.assertFalse(email_mfa.check_code(self.user, "login", code))


class SignInWithEmailCodeTest(TestCase):
    def setUp(self):
        self.user = _user("signin@example.com")
        EmailMfaEnrolment.objects.create(user=self.user)
        self.client = Client()
        mail.outbox = []

    def _password_login(self):
        self.client.post(
            reverse("login"),
            {"username": "signin@example.com", "password": PASSWORD},
        )

    def test_password_login_is_challenged_for_email_enrolled_users(self):
        resp = self.client.post(
            reverse("login"),
            {"username": "signin@example.com", "password": PASSWORD},
        )

        self.assertRedirects(resp, reverse("2fa-verify"), fetch_redirect_response=False)

    def test_emailed_code_signs_the_user_in(self):
        self._password_login()
        self.client.post(reverse("2fa-verify"), {"action": "send_email"})

        self.client.post(reverse("2fa-verify"), {"token": _last_code()})

        self.assertIn("_auth_user_id", self.client.session)
        self.assertNotIn(DEVICE_ID_SESSION_KEY, self.client.session)

    def test_wrong_emailed_code_does_not_sign_in(self):
        self._password_login()
        self.client.post(reverse("2fa-verify"), {"action": "send_email"})

        self.client.post(reverse("2fa-verify"), {"token": "000000"})

        self.assertNotIn("_auth_user_id", self.client.session)


@override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
class EmailNeverSatisfiesEnforcementTest(TestCase):
    def setUp(self):
        self.staff = _user("staffemail@example.com", staff=True)
        EmailMfaEnrolment.objects.create(user=self.staff)
        self.client = Client()
        mail.outbox = []

    def test_enforced_staff_with_only_email_must_pass_the_prompt_first(self):
        resp = self.client.post(
            reverse("login"),
            {"username": "staffemail@example.com", "password": PASSWORD},
        )

        self.assertEqual(resp.get("Location"), reverse("2fa-verify"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_enforced_staff_signs_in_with_email_then_is_sent_to_set_up(self):
        self.client.post(
            reverse("login"),
            {"username": "staffemail@example.com", "password": PASSWORD},
        )
        self.client.post(reverse("2fa-verify"), {"action": "send_email"})
        self.client.post(reverse("2fa-verify"), {"token": _last_code()})

        resp = self.client.get(reverse("settings-security"))

        self.assertRedirects(resp, reverse("2fa-setup"), fetch_redirect_response=False)

    def test_enforced_staff_with_only_email_is_sent_to_set_up_a_factor(self):
        self.client.force_login(self.staff)

        resp = self.client.get(reverse("settings-security"))

        self.assertRedirects(resp, reverse("2fa-setup"), fetch_redirect_response=False)


class EmailEnrolmentSettingsTest(TestCase):
    def setUp(self):
        self.user = _user("enrol@example.com")
        self.client = Client()
        self.client.force_login(self.user)
        mail.outbox = []

    def test_enrolling_needs_the_emailed_code(self):
        self.client.post(reverse("settings-email-mfa"), {"action": "send_enrol"})
        self.assertFalse(EmailMfaEnrolment.objects.filter(user=self.user).exists())

        self.client.post(
            reverse("settings-email-mfa"),
            {"action": "confirm_enrol", "password": PASSWORD, "token": _last_code()},
        )

        self.assertTrue(EmailMfaEnrolment.objects.filter(user=self.user).exists())

    def test_disabling_needs_password_and_emailed_code(self):
        EmailMfaEnrolment.objects.create(user=self.user)
        self.client.post(reverse("settings-email-mfa"), {"action": "send_disable"})

        self.client.post(
            reverse("settings-email-mfa"),
            {"action": "confirm_disable", "password": "wrong", "token": _last_code()},
        )
        self.assertTrue(EmailMfaEnrolment.objects.filter(user=self.user).exists())

        self.client.post(
            reverse("settings-email-mfa"),
            {"action": "confirm_disable", "password": PASSWORD, "token": _last_code()},
        )
        self.assertFalse(EmailMfaEnrolment.objects.filter(user=self.user).exists())

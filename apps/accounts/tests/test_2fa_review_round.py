"""Regression tests for the 2FA sign-in and recovery audit (10 findings).

Each class reproduces the failure scenario from the review, so it fails on the
code that shipped before the fix.
"""

from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import recovery
from apps.accounts.models import PasskeyCredential, RecoveryCode
from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_2fa_hardening import _enrolled
from apps.accounts.views import RECOVERY_DISPLAY_KEY, STRONG_VERIFIED_KEY

PASSWORD = "pw-12345678"


def _staff_with_app(email):
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_staff = True
    user.save()
    return user, _enrolled(user)


class Finding1EnforcedRecoveryPageTest(TestCase):
    """An enforced user who lost the phone can reach the recovery page and sign in."""

    @override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
    def test_the_step_up_prompt_leads_to_a_working_recovery_page(self):
        user, device = _staff_with_app("enforced-recovery@example.com")
        codes = recovery.generate_codes(user)
        client = Client()
        client.force_login(user)

        # The enforcement middleware sends an unverified session to the prompt.
        client.get(reverse("settings-security"))
        page = client.get(reverse("2fa-recovery"))

        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "auth/2fa_recovery.html")

        client.post(reverse("2fa-recovery"), {"code": codes[0]})

        self.assertIn(DEVICE_ID_SESSION_KEY, client.session)
        self.assertEqual(client.get(reverse("settings-security")).status_code, 200)

    @override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
    def test_the_recovery_page_does_not_verify_a_session_by_itself(self):
        user, _ = _staff_with_app("enforced-no-bypass@example.com")
        client = Client()
        client.force_login(user)
        client.get(reverse("settings-security"))

        client.get(reverse("2fa-recovery"))

        self.assertNotIn(DEVICE_ID_SESSION_KEY, client.session)


class Finding2RecoveryCodesInManagementFormsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="mgmt@example.com", email="mgmt@example.com", password=PASSWORD
        )
        self.device = _enrolled(self.user)
        self.codes = recovery.generate_codes(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_disable_form_accepts_a_full_length_recovery_code(self):
        page = self.client.get(reverse("2fa-setup"))

        self.assertContains(page, 'maxlength="24"')
        self.assertNotContains(page, 'maxlength="7"')

    def test_a_valid_recovery_code_turns_two_factor_off(self):
        self.client.post(
            reverse("2fa-setup"),
            {"action": "disable", "password": PASSWORD, "token": self.codes[0]},
        )

        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_an_invalid_recovery_code_cannot_turn_two_factor_off(self):
        self.client.post(
            reverse("2fa-setup"),
            {"action": "disable", "password": PASSWORD, "token": "AAAA-BBBB-CCCC-DDDD"},
        )

        self.assertTrue(TOTPDevice.objects.filter(user=self.user).exists())

    def test_a_valid_recovery_code_can_regenerate(self):
        self.client.post(
            reverse("recovery-codes-regenerate"),
            {"password": PASSWORD, "token": self.codes[0]},
        )

        self.assertFalse(recovery.redeem(self.user, self.codes[1]))

    def test_the_regenerate_form_is_shared_between_pages(self):
        page = self.client.get(reverse("2fa-setup"))
        self.assertContains(page, 'id="id_regen_token"')
        self.assertContains(page, 'maxlength="24"')


class Finding3RegenerationNeedsAnAppTest(TestCase):
    """A passkey-only account cannot be given recovery codes by an emailed code."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="nokeys@example.com", email="nokeys@example.com", password=PASSWORD
        )
        PasskeyCredential.objects.create(
            user=self.user,
            name="Key",
            credential_id=b"k",
            public_key=b"p",
            confirmed=True,
        )
        self.client = Client()
        self.client.force_login(self.user)
        mail.outbox = []

    def test_an_accepted_emailed_code_cannot_create_recovery_codes(self):
        # Even when the emailed code is accepted, the account has no app, so no codes.
        with mock.patch("apps.accounts.email_mfa.check_code", return_value=True):
            self.client.post(
                reverse("recovery-codes-regenerate"),
                {"password": PASSWORD, "token": "123456"},
            )

        self.assertFalse(RecoveryCode.objects.filter(user=self.user).exists())
        self.assertEqual(mail.outbox, [])  # no owner notice for a refused change

    def test_the_expired_page_offers_no_regenerate_form_without_an_app(self):
        resp = self.client.get(reverse("settings-recovery-codes"))

        self.assertNotContains(resp, 'id="id_regen_token"')
        self.assertContains(resp, "Turn on an authenticator app")


class Finding4PasswordReauthThrottleTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="guess@example.com", email="guess@example.com", password=PASSWORD
        )
        self.device = _enrolled(self.user)
        self.codes = recovery.generate_codes(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_repeated_wrong_passwords_lock_out_even_the_right_password(self):
        for _ in range(5):
            self.client.post(
                reverse("recovery-codes-regenerate"),
                {"password": "wrong", "token": _current_token(self.device)},
            )

        resp = self.client.post(
            reverse("recovery-codes-regenerate"),
            {"password": PASSWORD, "token": _current_token(self.device)},
            follow=True,
        )

        self.assertContains(resp, "Too many failed attempts")
        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT)

    def test_a_correct_password_clears_the_count(self):
        for _ in range(3):
            self.client.post(
                reverse("2fa-setup"),
                {"action": "disable", "password": "wrong", "token": "000000"},
            )

        self.client.post(
            reverse("2fa-setup"),
            {
                "action": "disable",
                "password": PASSWORD,
                "token": _current_token(self.device),
            },
        )

        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())


class Finding5SessionHandlingTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="sessions@example.com",
            email="sessions@example.com",
            password=PASSWORD,
        )
        self.device = _enrolled(self.user)

    def test_unrelated_session_data_survives_sign_in(self):
        client = Client()
        session = client.session
        session["language"] = "fr"
        session.save()

        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )
        client.post(reverse("2fa-verify"), {"token": _current_token(self.device)})

        self.assertEqual(client.session.get("language"), "fr")

    def test_a_planted_recovery_display_is_cleared(self):
        client = Client()
        session = client.session
        session[RECOVERY_DISPLAY_KEY] = "stale"
        session[STRONG_VERIFIED_KEY] = {
            "at": timezone.now().isoformat(),
            "user": self.user.pk,
        }
        session.save()

        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )

        self.assertNotIn(RECOVERY_DISPLAY_KEY, client.session)
        self.assertNotIn(STRONG_VERIFIED_KEY, client.session)


class Finding6PendingUserTest(TestCase):
    """A pending pointer to a deleted user is cleared by the one helper that reads it."""

    def test_the_sign_in_step_clears_a_stale_pointer(self):
        client = Client()
        session = client.session
        session["_2fa_user_pk"] = 987654
        session.save()

        resp = client.get(reverse("2fa-verify"))

        self.assertRedirects(resp, settings.LOGIN_URL, fetch_redirect_response=False)
        self.assertNotIn("_2fa_user_pk", client.session)

    def test_the_recovery_step_clears_a_stale_pointer_too(self):
        client = Client()
        session = client.session
        session["_2fa_user_pk"] = 987654
        session.save()

        resp = client.get(reverse("2fa-recovery"))

        self.assertRedirects(resp, reverse("2fa-verify"), fetch_redirect_response=False)
        self.assertNotIn("_2fa_user_pk", client.session)


class Finding7SingleLockCheckTest(TestCase):
    """The lockout is read once, inside the same lock as the check."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="lock-once@example.com",
            email="lock-once@example.com",
            password=PASSWORD,
        )
        self.device = _enrolled(self.user)
        self.client = Client()
        self.client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )

    def test_a_locked_device_reports_the_wait_and_is_not_counted_again(self):
        self.device.throttling_failure_count = 1
        self.device.throttling_failure_timestamp = timezone.now()
        self.device.save()

        resp = self.client.post(reverse("2fa-verify"), {"token": "000000"}, follow=True)

        self.assertContains(resp, "Too many attempts. Try again in")
        self.device.refresh_from_db()
        self.assertEqual(self.device.throttling_failure_count, 1)

    def test_the_check_returns_the_wait_from_inside_the_lock(self):
        from apps.accounts.views import _check_second_factor

        self.device.throttling_failure_count = 3
        self.device.throttling_failure_timestamp = timezone.now()
        self.device.save()

        factor, wait = _check_second_factor(
            self.user, self.device, _current_token(self.device)
        )

        self.assertIsNone(factor)
        self.assertIn("Too many attempts", wait)


class Finding8SharedFormTest(TestCase):
    def test_both_pages_render_the_same_regenerate_form(self):
        from django.template.loader import render_to_string

        form = render_to_string("accounts/_recovery_regenerate_form.html")

        self.assertIn('name="token"', form)
        self.assertIn('maxlength="24"', form)
        self.assertEqual(
            Path(settings.BASE_DIR, "templates/accounts/settings_2fa.html")
            .read_text(encoding="utf-8")
            .count("_recovery_regenerate_form.html"),
            1,
        )


class Finding9NoCountBeforeSecondFactorTest(TestCase):
    def test_a_password_only_visitor_sees_no_recovery_count(self):
        user = User.objects.create_user(
            username="count@example.com", email="count@example.com", password=PASSWORD
        )
        _enrolled(user)
        recovery.generate_codes(user)
        client = Client()
        client.post(reverse("login"), {"username": user.email, "password": PASSWORD})

        resp = client.get(reverse("2fa-recovery"))

        self.assertNotContains(resp, "You have")
        self.assertNotContains(resp, "codes left")


class Finding10PasskeyMessageTest(TestCase):
    def test_a_not_allowed_error_does_not_claim_the_user_cancelled(self):
        script = Path(settings.BASE_DIR, "static/js/passkeys.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("may not be on this device", script)
        self.assertNotIn('"Cancelled. Try again when you\'re ready."', script)

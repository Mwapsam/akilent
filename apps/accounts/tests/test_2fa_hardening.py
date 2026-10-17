"""PR-2 regression tests: recovery codes, verify lockout, re-auth to disable."""

from django.contrib.auth import get_user
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import recovery
from apps.accounts.models import RecoveryCode
from apps.accounts.tests.test_2fa import _current_token

PASSWORD = "pw-12345678"


def _user(email="pr2@example.com"):
    return User.objects.create_user(username=email, email=email, password=PASSWORD)


def _enrolled(user):
    return TOTPDevice.objects.create(user=user, name="default", confirmed=True)


def _login_to_second_step(client, email="pr2@example.com"):
    client.post(reverse("login"), {"username": email, "password": PASSWORD})


class RecoveryCodeTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.device = _enrolled(self.user)
        self.client = Client()

    def test_recovery_code_signs_in_and_marks_session_otp_verified(self):
        codes = recovery.generate_codes(self.user)
        _login_to_second_step(self.client)

        resp = self.client.post(reverse("2fa-recovery"), {"code": codes[0]})

        self.assertTrue(get_user(self.client).is_authenticated)
        self.assertEqual(
            self.client.session[DEVICE_ID_SESSION_KEY], self.device.persistent_id
        )
        self.assertEqual(resp.status_code, 302)

    def test_recovery_code_cannot_be_reused(self):
        codes = recovery.generate_codes(self.user)
        self.assertTrue(recovery.redeem(self.user, codes[0]))

        self.assertFalse(recovery.redeem(self.user, codes[0]))

    def test_recovery_code_accepts_any_case_and_separators(self):
        codes = recovery.generate_codes(self.user)
        spaced = codes[1].lower().replace("-", " ")

        self.assertTrue(recovery.redeem(self.user, spaced))

    def test_codes_are_stored_as_digests_not_plaintext(self):
        codes = recovery.generate_codes(self.user)

        stored = set(RecoveryCode.objects.values_list("code_hash", flat=True))
        self.assertEqual(len(stored), recovery.CODE_COUNT)
        for code in codes:
            self.assertNotIn(code, stored)
            self.assertNotIn(code.replace("-", ""), stored)

    def test_regenerating_codes_invalidates_the_old_set(self):
        old = recovery.generate_codes(self.user)
        recovery.generate_codes(self.user)

        self.assertFalse(recovery.redeem(self.user, old[0]))

    def test_wrong_code_is_rejected_and_counts_against_lockout(self):
        recovery.generate_codes(self.user)
        _login_to_second_step(self.client)

        self.client.post(reverse("2fa-recovery"), {"code": "AAAA-BBBB-CCCC-DDDD"})

        self.device.refresh_from_db()
        self.assertEqual(self.device.throttling_failure_count, 1)
        self.assertFalse(get_user(self.client).is_authenticated)


class TotpVerifyHardeningTest(TestCase):
    def setUp(self):
        self.user = _user("totp@example.com")
        self.device = _enrolled(self.user)
        self.client = Client()

    def test_totp_code_cannot_be_replayed(self):
        token = _current_token(self.device)
        self.assertTrue(self.device.verify_token(token))

        self.assertFalse(self.device.verify_token(token))

    def test_locked_device_rejects_a_correct_totp_code(self):
        from django.utils import timezone

        self.device.throttling_failure_count = 5
        self.device.throttling_failure_timestamp = timezone.now()
        self.device.save()
        _login_to_second_step(self.client, "totp@example.com")

        self.client.post(reverse("2fa-verify"), {"token": _current_token(self.device)})

        self.assertFalse(get_user(self.client).is_authenticated)

    def test_locked_device_rejects_a_correct_recovery_code(self):
        from django.utils import timezone

        codes = recovery.generate_codes(self.user)
        self.device.throttling_failure_count = 5
        self.device.throttling_failure_timestamp = timezone.now()
        self.device.save()
        _login_to_second_step(self.client, "totp@example.com")

        self.client.post(reverse("2fa-verify"), {"token": codes[0]})

        self.assertFalse(get_user(self.client).is_authenticated)
        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT)


class DisableReauthTest(TestCase):
    def setUp(self):
        self.user = _user("disable@example.com")
        self.device = _enrolled(self.user)
        recovery.generate_codes(self.user)
        self.client = Client()
        self.client.force_login(self.user)
        self.url = reverse("2fa-setup")

    def _disable(self, password, token):
        return self.client.post(
            self.url, {"action": "disable", "password": password, "token": token}
        )

    def test_disable_without_password_is_refused(self):
        self._disable("wrong-password", _current_token(self.device))

        self.assertTrue(TOTPDevice.objects.filter(pk=self.device.pk).exists())

    def test_disable_with_password_but_no_valid_factor_is_refused(self):
        self._disable(PASSWORD, "000000")

        self.assertTrue(TOTPDevice.objects.filter(pk=self.device.pk).exists())

    def test_disable_with_password_and_code_removes_factor_and_recovery_codes(self):
        self._disable(PASSWORD, _current_token(self.device))

        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())
        self.assertFalse(RecoveryCode.objects.filter(user=self.user).exists())


class EnableIssuesRecoveryCodesTest(TestCase):
    def setUp(self):
        self.user = _user("enable@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_enable_shows_ten_recovery_codes_once(self):
        device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=False
        )

        resp = self.client.post(
            reverse("2fa-setup"),
            {"action": "enable", "token": _current_token(device)},
        )
        self.assertRedirects(
            resp, reverse("settings-recovery-codes"), fetch_redirect_response=False
        )

        page = self.client.get(reverse("settings-recovery-codes"))
        self.assertTemplateUsed(page, "accounts/2fa_recovery_codes.html")
        self.assertEqual(len(page.context["codes"]), recovery.CODE_COUNT)
        self.assertEqual(
            len(self.client.get(reverse("settings-recovery-codes")).context["codes"]),
            recovery.CODE_COUNT,
        )
        self.assertEqual(
            RecoveryCode.objects.filter(user=self.user).count(), recovery.CODE_COUNT
        )


class SpacedCodeTest(TestCase):
    """A code typed as '123 456' (the placeholder's own format) must verify."""

    def test_spaced_totp_code_signs_in(self):
        from django_otp import DEVICE_ID_SESSION_KEY

        user = _user("spaced@example.com")
        device = _enrolled(user)
        client = Client()
        _login_to_second_step(client, "spaced@example.com")
        code = _current_token(device)
        spaced = f"{code[:3]} {code[3:]}"

        client.post(reverse("2fa-verify"), {"token": spaced})

        self.assertEqual(client.session[DEVICE_ID_SESSION_KEY], device.persistent_id)

    def test_spaced_code_on_disable_is_accepted(self):
        user = _user("spaced-disable@example.com")
        device = _enrolled(user)
        recovery.generate_codes(user)
        client = Client()
        client.force_login(user)
        code = _current_token(device)

        client.post(
            reverse("2fa-setup"),
            {
                "action": "disable",
                "password": PASSWORD,
                "token": f"{code[:3]} {code[3:]}",
            },
        )

        self.assertFalse(TOTPDevice.objects.filter(pk=device.pk).exists())


class EnrolmentUriIssuerTest(TestCase):
    """Authenticator apps must show the app name next to the account."""

    def test_enrolment_uri_names_the_issuer(self):
        from urllib.parse import unquote, urlsplit

        user = _user("issuer@example.com")
        device = TOTPDevice.objects.create(user=user, name="default", confirmed=False)

        uri = unquote(urlsplit(device.config_url).path)
        query = device.config_url

        self.assertIn("Akilent:issuer@example.com", uri)
        self.assertIn("issuer=Akilent", query)

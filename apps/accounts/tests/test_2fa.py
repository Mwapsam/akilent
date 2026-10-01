"""Tests for two-factor authentication (TOTP) login flow and setup."""

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse


def _current_token(device):
    """Generate the current valid TOTP token for a device."""
    import time as _time

    from django_otp.oath import TOTP

    totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
    totp.time = _time.time()
    return str(totp.token()).zfill(device.digits)


def _make_user(email="alice@example.com", password="testpass123"):
    u = User.objects.create_user(username=email, email=email, password=password)
    u.backend = "apps.accounts.backends.EmailBackend"
    return u


class TwoFALoginFlowTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = _make_user()

    def _post_login(self, **kwargs):
        return self.client.post(
            reverse("login"),
            {"username": "alice@example.com", "password": "testpass123", **kwargs},
            follow=False,
        )

    def test_login_without_2fa_goes_directly_to_dashboard(self):
        resp = self._post_login()
        self.assertNotEqual(resp.url, reverse("2fa-verify"))

    def test_login_with_2fa_redirects_to_verify(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        resp = self._post_login()
        self.assertRedirects(resp, reverse("2fa-verify"), fetch_redirect_response=False)

    def test_verify_with_valid_token_logs_user_in(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )
        # Prime the session via the login POST.
        self._post_login()
        token = _current_token(device)
        resp = self.client.post(
            reverse("2fa-verify"),
            {"token": token},
            follow=False,
        )
        self.assertEqual(resp.status_code, 302)
        # User should now be authenticated.
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_verify_with_invalid_token_shows_error(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        self._post_login()
        resp = self.client.post(
            reverse("2fa-verify"),
            {"token": "000000"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_verify_with_no_session_redirects_to_login(self):
        resp = self.client.get(reverse("2fa-verify"))
        self.assertRedirects(resp, reverse("login"), fetch_redirect_response=False)


class TwoFASetupTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)

    def test_setup_page_shows_qr_when_no_device(self):
        resp = self.client.get(reverse("2fa-setup"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "data:image/svg+xml;base64,")

    def test_enable_with_valid_token_confirms_device(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        # GET to create the unconfirmed device.
        self.client.get(reverse("2fa-setup"))
        device = TOTPDevice.objects.get(user=self.user, confirmed=False)
        token = _current_token(device)
        resp = self.client.post(
            reverse("2fa-setup"),
            {"action": "enable", "token": str(token)},
            follow=False,
        )
        self.assertRedirects(
            resp, reverse("settings-security"), fetch_redirect_response=False
        )
        self.assertTrue(
            TOTPDevice.objects.filter(user=self.user, confirmed=True).exists()
        )

    def test_enable_with_invalid_token_stays_on_setup(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        self.client.get(reverse("2fa-setup"))
        resp = self.client.post(
            reverse("2fa-setup"),
            {"action": "enable", "token": "000000"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            TOTPDevice.objects.filter(user=self.user, confirmed=True).exists()
        )

    def test_disable_removes_device(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        resp = self.client.post(
            reverse("2fa-setup"),
            {"action": "disable"},
            follow=False,
        )
        self.assertRedirects(
            resp, reverse("settings-security"), fetch_redirect_response=False
        )
        self.assertFalse(TOTPDevice.objects.filter(user=self.user).exists())

    def test_security_page_shows_totp_enabled_when_active(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        resp = self.client.get(reverse("settings-security"))
        self.assertContains(resp, "Enabled")

"""Adding a passkey on the current device, using the same one-method choice as QR."""

import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import PasskeyCredential
from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_passkeys import _fake_registration

PASSWORD = "pw-12345678"
REG_VERIFY = "apps.accounts.passkeys.verify_registration_response"


def _user(email, staff=False):
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_staff = staff
    user.save()
    return user


def _options(client, payload):
    return client.post(
        reverse("passkey-register-options"),
        data=json.dumps(payload),
        content_type="application/json",
    )


class SameDevicePageTest(TestCase):
    def setUp(self):
        cache.clear()
        self.client = Client()
        self.client.force_login(_user("page@example.com"))

    def test_the_add_button_starts_the_browser_ceremony(self):
        resp = self.client.get(reverse("settings-passkeys"))

        self.assertContains(resp, 'id="passkey-add-here"')
        self.assertContains(resp, "passkeyRegister(")

    def test_the_page_has_no_custom_qr_or_phone_links(self):
        resp = self.client.get(reverse("settings-passkeys"))

        self.assertNotContains(resp, "QR code")
        self.assertNotContains(resp, "/passkey/link/")
        self.assertNotContains(resp, "passkey-link")


class SameDeviceRegistrationTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("same-device@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_password_is_accepted_on_its_own_when_mfa_is_not_required(self):
        resp = _options(self.client, {"method": "password", "password": PASSWORD})

        self.assertEqual(resp.status_code, 200)
        self.assertIn("challenge", resp.json())

    def test_a_wrong_password_is_refused(self):
        resp = _options(self.client, {"method": "password", "password": "wrong"})

        self.assertEqual(resp.status_code, 400)
        self.assertIn("didn't match", resp.json()["error"])

    def test_a_chosen_app_code_is_accepted_without_the_password(self):
        device = TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)

        resp = _options(
            self.client, {"method": "app", "token": _current_token(device)}
        )

        self.assertEqual(resp.status_code, 200)

    def test_no_method_is_refused(self):
        resp = _options(self.client, {"password": PASSWORD})

        self.assertEqual(resp.status_code, 400)

    @override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
    def test_password_and_email_are_refused_where_mfa_is_required(self):
        staff = _user("same-device-staff@example.com", staff=True)
        client = Client()
        client.force_login(staff)

        by_password = _options(client, {"method": "password", "password": PASSWORD})
        by_email = _options(client, {"method": "email", "token": "123456"})

        self.assertEqual(by_password.status_code, 400)
        self.assertEqual(by_email.status_code, 400)


class SameDeviceFinishTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("finish-device@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_a_stored_passkey_sends_the_owner_a_notice(self):
        _options(self.client, {"method": "password", "password": PASSWORD})

        with mock.patch(REG_VERIFY, return_value=_fake_registration(b"device")), mock.patch(
            "apps.accounts.security_notices.notify_after_commit"
        ) as notify:
            resp = self.client.post(
                reverse("passkey-register-finish"),
                data=json.dumps({"credential": {}, "name": "Laptop"}),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(PasskeyCredential.objects.filter(user=self.user).exists())
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], self.user)

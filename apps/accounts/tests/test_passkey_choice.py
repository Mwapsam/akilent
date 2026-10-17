"""Adding a passkey: the user picks one way to confirm it's them, then the browser ceremony starts."""

import json

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import PasskeyRegistrationChallenge
from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_email_mfa import _last_code

PASSWORD = "pw-12345678"


def _user(email, staff=False):
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_staff = staff
    user.save()
    return user


class PasskeyChoiceTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("choice@example.com")
        self.client = Client()
        self.client.force_login(self.user)
        mail.outbox = []

    def _start(self, **data):
        return self.client.post(
            reverse("passkey-register-options"),
            data=json.dumps(data),
            content_type="application/json",
        )

    def test_password_alone_is_enough_for_an_account_without_mfa(self):
        resp = self._start(method="password", password=PASSWORD)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("challenge", resp.json())

    def test_the_app_code_is_enough_without_the_password(self):
        device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )

        resp = self._start(method="app", token=_current_token(device))

        self.assertEqual(resp.status_code, 200)

    def test_an_emailed_code_is_enough_without_the_password(self):
        self.client.post(reverse("passkey-reauth-send"))

        resp = self._start(method="email", token=_last_code())

        self.assertEqual(resp.status_code, 200)

    def test_a_wrong_app_code_is_refused(self):
        TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)

        resp = self._start(method="app", token="000000")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyRegistrationChallenge.objects.exists())

    def test_no_method_chosen_is_refused(self):
        resp = self._start(token="000000")

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyRegistrationChallenge.objects.exists())

    @override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
    def test_password_alone_is_refused_where_mfa_is_required(self):
        staff = _user("choice-staff@example.com", staff=True)
        client = Client()
        client.force_login(staff)

        resp = client.post(
            reverse("passkey-register-options"),
            data=json.dumps({"method": "password", "password": PASSWORD}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyRegistrationChallenge.objects.exists())

    def test_the_enrolment_page_offers_password_only_when_allowed(self):
        resp = self.client.get(reverse("settings-passkeys"))

        self.assertContains(resp, 'value="password"')
        self.assertContains(resp, "Email code")

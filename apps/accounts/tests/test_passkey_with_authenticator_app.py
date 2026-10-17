"""An account with an authenticator app (Authy or similar) can still add a passkey."""

import json
import time
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_2fa_hardening import _enrolled
from apps.accounts.views import STRONG_VERIFIED_KEY

PASSWORD = "pw-12345678"


class PasskeyWithAppEnrolledTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="authy@example.com", email="authy@example.com", password=PASSWORD
        )
        self.device = _enrolled(self.user)

    def _sign_in_with_app(self):
        client = Client()
        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )
        client.post(reverse("2fa-verify"), {"token": _current_token(self.device)})
        return client

    def test_the_settings_page_loads_and_offers_the_add_button(self):
        client = self._sign_in_with_app()

        resp = client.get(reverse("settings-passkeys"))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="passkey-add-here"')

    def test_a_recent_sign_in_with_the_app_starts_registration_without_a_new_code(self):
        client = self._sign_in_with_app()

        resp = client.post(
            reverse("passkey-register-options"),
            data="{}",
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertIn("challenge", json.loads(resp.content))

    def _expire_sign_in_stamp(self, client):
        session = client.session
        stamp = session[STRONG_VERIFIED_KEY]
        stamp["at"] = (timezone.now() - timedelta(hours=1)).isoformat()
        session[STRONG_VERIFIED_KEY] = stamp
        session.save()

    def test_the_code_used_to_sign_in_is_refused_with_a_clear_reason(self):
        client = self._sign_in_with_app()
        self._expire_sign_in_stamp(client)

        resp = client.post(
            reverse("passkey-register-options"),
            data=json.dumps({"method": "app", "token": _current_token(self.device)}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("just used to sign in", resp.json()["error"])

    def test_the_next_app_code_works_after_an_old_sign_in(self):
        client = self._sign_in_with_app()
        self._expire_sign_in_stamp(client)
        from django_otp.oath import TOTP

        from apps.accounts import totp_storage

        totp_storage.load(self.device)
        next_step = TOTP(
            self.device.bin_key,
            self.device.step,
            self.device.t0,
            self.device.digits,
            self.device.drift,
        )
        next_step.time = time.time() + self.device.step
        next_code = f"{next_step.token():06d}"

        resp = client.post(
            reverse("passkey-register-options"),
            data=json.dumps({"method": "app", "token": next_code}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200, resp.content[:300])

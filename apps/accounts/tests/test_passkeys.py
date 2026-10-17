"""Passkeys: registration, sign-in, cloned-key refusal, removal, and enforcement.

The WebAuthn cryptographic checks are mocked here. These tests cover our own
rules: challenges are single-use and expire, a bad response is refused, a
counter that does not advance is refused, removal needs re-authentication, and
a passkey counts as a second factor. A real authenticator still needs a manual
check on a device.
"""

import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY

from apps.accounts import passkeys
from apps.accounts.mfa import has_second_factor
from apps.accounts.models import PasskeyCredential
from apps.accounts.tests.test_email_mfa import _last_code

PASSWORD = "pw-12345678"
REG_VERIFY = "apps.accounts.passkeys.verify_registration_response"
AUTH_VERIFY = "apps.accounts.passkeys.verify_authentication_response"


def _user(email, staff=False):
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_staff = staff
    user.save()
    return user


def _b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _passkey(user, cred_id=b"cred-1", sign_count=0):
    return PasskeyCredential.objects.create(
        user=user,
        name="Laptop",
        credential_id=cred_id,
        public_key=b"public-key",
        sign_count=sign_count,
        confirmed=True,
    )


def _fake_registration(cred_id=b"new-cred"):
    return SimpleNamespace(
        credential_id=cred_id, credential_public_key=b"public-key", sign_count=0
    )


def _fake_authentication(new_sign_count):
    return SimpleNamespace(new_sign_count=new_sign_count)


def _post_json(client, name, payload):
    return client.post(
        reverse(name), data=json.dumps(payload), content_type="application/json"
    )


class PasskeyRegistrationTest(TestCase):
    def setUp(self):
        self.user = _user("reg@example.com")
        self.client = Client()
        self.client.force_login(self.user)
        mail.outbox = []

    def _start_registration(self):
        self.client.post(reverse("passkey-reauth-send"))
        return _post_json(
            self.client,
            "passkey-register-options",
            {"password": PASSWORD, "token": _last_code()},
        )

    def test_registration_needs_the_password(self):
        resp = _post_json(
            self.client, "passkey-register-options", {"password": "wrong"}
        )

        self.assertEqual(resp.status_code, 400)

    def test_finish_without_recent_reauthentication_is_refused(self):
        with mock.patch(REG_VERIFY, return_value=_fake_registration()):
            resp = _post_json(
                self.client, "passkey-register-finish", {"credential": {}}
            )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.filter(user=self.user).exists())

    def test_options_then_finish_stores_the_passkey(self):
        resp = self._start_registration()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("challenge", resp.json())

        with mock.patch(REG_VERIFY, return_value=_fake_registration()):
            done = _post_json(
                self.client,
                "passkey-register-finish",
                {"credential": {"id": "x"}, "name": "Work"},
            )

        self.assertEqual(done.status_code, 200)
        self.assertTrue(
            PasskeyCredential.objects.filter(user=self.user, name="Work").exists()
        )

    def test_challenge_is_single_use(self):
        self._start_registration()
        with mock.patch(REG_VERIFY, return_value=_fake_registration()):
            _post_json(self.client, "passkey-register-finish", {"credential": {}})

        with mock.patch(REG_VERIFY, return_value=_fake_registration(b"again")):
            again = _post_json(
                self.client, "passkey-register-finish", {"credential": {}}
            )

        self.assertEqual(again.status_code, 400)

    def test_expired_challenge_is_refused(self):
        self._start_registration()
        session = self.client.session
        session[passkeys.REG_KEY]["at"] = (
            timezone.now() - timedelta(minutes=10)
        ).isoformat()
        session.save()

        with mock.patch(REG_VERIFY, return_value=_fake_registration()):
            resp = _post_json(
                self.client, "passkey-register-finish", {"credential": {}}
            )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.filter(user=self.user).exists())

    def test_a_response_that_does_not_verify_stores_nothing(self):
        self._start_registration()
        with mock.patch(REG_VERIFY, side_effect=ValueError("bad origin")):
            resp = _post_json(
                self.client, "passkey-register-finish", {"credential": {}}
            )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.filter(user=self.user).exists())


class PasskeyInputTest(TestCase):
    def test_non_json_body_is_a_400_not_a_500(self):
        user = _user("badjson@example.com")
        client = Client()
        client.force_login(user)

        resp = client.post(
            reverse("passkey-register-finish"),
            data="not json",
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)


class PasskeySignInTest(TestCase):
    def setUp(self):
        self.user = _user("signin@example.com")
        self.passkey = _passkey(self.user, sign_count=3)
        self.client = Client()
        self.client.post(
            reverse("login"),
            {"username": "signin@example.com", "password": PASSWORD},
        )

    def _assertion(self, cred_id=b"cred-1"):
        return {"credential": {"id": _b64u(cred_id), "rawId": _b64u(cred_id)}}

    def test_passkey_completes_sign_in_and_marks_the_session(self):
        self.client.post(reverse("passkey-signin-options"))

        with mock.patch(AUTH_VERIFY, return_value=_fake_authentication(4)):
            resp = _post_json(self.client, "passkey-signin-finish", self._assertion())

        self.assertEqual(resp.status_code, 200)
        self.assertIn("redirect", resp.json())
        self.assertIn("_auth_user_id", self.client.session)
        self.assertEqual(
            self.client.session[DEVICE_ID_SESSION_KEY], self.passkey.persistent_id
        )
        self.passkey.refresh_from_db()
        self.assertEqual(self.passkey.sign_count, 4)

    def test_unknown_credential_is_refused(self):
        self.client.post(reverse("passkey-signin-options"))

        with mock.patch(AUTH_VERIFY, return_value=_fake_authentication(4)):
            resp = _post_json(
                self.client, "passkey-signin-finish", self._assertion(b"someone-else")
            )

        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_counter_that_does_not_advance_is_refused_as_a_clone(self):
        self.client.post(reverse("passkey-signin-options"))

        # The library raises when the counter does not advance; we treat that as a refusal.
        with mock.patch(
            AUTH_VERIFY, side_effect=ValueError("sign count did not advance")
        ):
            resp = _post_json(self.client, "passkey-signin-finish", self._assertion())

        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_sign_in_options_are_refused_without_a_pending_login(self):
        fresh = Client()

        resp = fresh.post(reverse("passkey-signin-options"))

        self.assertEqual(resp.status_code, 400)


class PasskeyRemovalTest(TestCase):
    def setUp(self):
        self.user = _user("remove@example.com")
        self.passkey = _passkey(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_wrong_password_keeps_the_passkey(self):
        self.client.post(
            reverse("passkey-remove", args=[self.passkey.pk]), {"password": "nope"}
        )

        self.assertTrue(PasskeyCredential.objects.filter(pk=self.passkey.pk).exists())

    def test_right_password_and_emailed_code_removes_the_passkey(self):
        self.client.post(reverse("passkey-reauth-send"))
        self.client.post(
            reverse("passkey-remove", args=[self.passkey.pk]),
            {"password": PASSWORD, "token": _last_code()},
        )

        self.assertFalse(PasskeyCredential.objects.filter(pk=self.passkey.pk).exists())

    def test_cannot_remove_another_users_passkey(self):
        other = _user("other@example.com")
        theirs = _passkey(other, cred_id=b"theirs")

        self.client.post(
            reverse("passkey-remove", args=[theirs.pk]), {"password": PASSWORD}
        )

        self.assertTrue(PasskeyCredential.objects.filter(pk=theirs.pk).exists())


@override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
class PasskeyEnforcementTest(TestCase):
    def test_a_passkey_satisfies_mandatory_mfa(self):
        staff = _user("staffkey@example.com", staff=True)
        _passkey(staff, cred_id=b"staff-key")
        self.assertTrue(has_second_factor(staff))

        client = Client()
        client.post(
            reverse("login"),
            {"username": "staffkey@example.com", "password": PASSWORD},
        )
        client.post(reverse("passkey-signin-options"))
        with mock.patch(AUTH_VERIFY, return_value=_fake_authentication(1)):
            _post_json(
                client,
                "passkey-signin-finish",
                {"credential": {"rawId": _b64u(b"staff-key")}},
            )

        resp = client.get(reverse("settings-security"))

        self.assertEqual(resp.status_code, 200)

"""QR enrolment: a desktop shows a code, the phone adds the passkey, the code works once."""

import base64
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import passkeys
from apps.accounts.models import (
    EmailMfaEnrolment,
    PasskeyCredential,
    PasskeyLinkRequest,
)
from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_email_mfa import _last_code
from apps.accounts.views import (
    PASSKEY_REAUTH_KEY,
    STRONG_VERIFIED_KEY,
    STRONG_VERIFIED_SECONDS,
    _recently_verified,
)

PASSWORD = "pw-12345678"
VERIFY = "apps.accounts.passkeys.verify_registration_response"


def _user(email):
    return User.objects.create_user(username=email, email=email, password=PASSWORD)


def _fresh_reauth(client):
    session = client.session
    session[PASSKEY_REAUTH_KEY] = timezone.now().isoformat()
    session.save()


def _pending_registration(client):
    session = client.session
    session[passkeys.REG_KEY] = {
        "challenge": base64.b64encode(b"chal").decode(),
        "at": timezone.now().isoformat(),
    }
    session.save()


def _verified(cred_id=b"phone-key"):
    return mock.Mock(credential_id=cred_id, credential_public_key=b"pk", sign_count=0)


class StartTest(TestCase):
    def setUp(self):
        self.user = _user("qr-start@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_wrong_password_creates_no_link(self):
        resp = self.client.post(
            reverse("passkey-link-start"), {"password": "wrong", "token": "000000"}
        )

        self.assertRedirects(
            resp, reverse("settings-passkeys"), fetch_redirect_response=False
        )
        self.assertFalse(PasskeyLinkRequest.objects.exists())

    def test_fresh_reauth_opens_the_qr_page(self):
        _fresh_reauth(self.client)

        resp = self.client.post(reverse("passkey-link-start"))

        self.assertEqual(PasskeyLinkRequest.objects.count(), 1)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/settings/security/passkeys/link/", resp["Location"])

    def test_the_stored_row_holds_a_digest_not_the_token(self):
        _fresh_reauth(self.client)
        resp = self.client.post(reverse("passkey-link-start"))
        token = resp["Location"].rstrip("/").rsplit("/", 1)[-1]

        link = PasskeyLinkRequest.objects.get()

        self.assertNotEqual(link.token_digest, token)
        self.assertEqual(len(link.token_digest), 64)

    def test_qr_page_renders_an_svg_for_the_phone_url(self):
        _fresh_reauth(self.client)
        resp = self.client.post(reverse("passkey-link-start"))

        page = self.client.get(resp["Location"])

        self.assertContains(page, "<svg")
        self.assertContains(page, "Waiting for your phone")


class PhoneUseTest(TestCase):
    def setUp(self):
        self.user = _user("qr-phone@example.com")
        self.other = _user("qr-other@example.com")
        self.token = passkeys.create_link(self.user)
        self.url = reverse("passkey-phone", args=[self.token])

    def test_anonymous_phone_is_sent_to_sign_in_first(self):
        resp = Client().get(self.url)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_another_account_cannot_use_the_code(self):
        client = Client()
        client.force_login(self.other)

        resp = client.get(self.url)

        self.assertContains(resp, "is for another account")
        self.assertNotContains(resp, "passkey-phone-btn")

    def test_another_account_cannot_get_registration_options(self):
        client = Client()
        client.force_login(self.other)

        resp = client.post(reverse("passkey-phone-options", args=[self.token]))

        self.assertEqual(resp.status_code, 400)

    def test_the_owner_gets_registration_options(self):
        client = Client()
        client.force_login(self.user)

        resp = client.post(reverse("passkey-phone-options", args=[self.token]))

        self.assertEqual(resp.status_code, 200)
        self.assertIn("challenge", json.loads(resp.content))


class CompleteLinkTest(TestCase):
    def setUp(self):
        self.user = _user("qr-complete@example.com")
        self.token = passkeys.create_link(self.user)
        self.client = Client()
        self.client.force_login(self.user)
        _pending_registration(self.client)
        self.url = reverse("passkey-phone-finish", args=[self.token])

    def test_a_verified_phone_passkey_is_stored_and_the_link_used(self):
        with mock.patch(VERIFY, return_value=_verified()):
            resp = self.client.post(
                self.url, {"credential": {"id": "x"}}, content_type="application/json"
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(PasskeyCredential.objects.filter(user=self.user).count(), 1)
        self.assertIsNotNone(PasskeyLinkRequest.objects.get().used_at)

    def test_the_same_code_cannot_be_used_twice(self):
        with mock.patch(VERIFY, return_value=_verified(b"first")):
            self.client.post(
                self.url, {"credential": {}}, content_type="application/json"
            )
        _pending_registration(self.client)

        with mock.patch(VERIFY, return_value=_verified(b"second")):
            resp = self.client.post(
                self.url, {"credential": {}}, content_type="application/json"
            )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PasskeyCredential.objects.filter(user=self.user).count(), 1)

    def test_a_failed_check_stores_nothing_and_keeps_the_code_usable(self):
        with mock.patch(VERIFY, side_effect=ValueError("bad")):
            resp = self.client.post(
                self.url, {"credential": {}}, content_type="application/json"
            )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.filter(user=self.user).exists())
        self.assertIsNone(PasskeyLinkRequest.objects.get().used_at)

    def test_an_expired_code_is_refused(self):
        PasskeyLinkRequest.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        with mock.patch(VERIFY, return_value=_verified()):
            resp = self.client.post(
                self.url, {"credential": {}}, content_type="application/json"
            )

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.exists())


class StatusTest(TestCase):
    def setUp(self):
        self.user = _user("qr-status@example.com")
        self.other = _user("qr-status-other@example.com")
        self.token = passkeys.create_link(self.user)
        self.url = reverse("passkey-link-status", args=[self.token])

    def test_pending_code_is_not_done(self):
        client = Client()
        client.force_login(self.user)

        resp = client.get(self.url)

        self.assertEqual(resp.json(), {"done": False, "expired": False})

    def test_a_used_code_reports_done_with_a_redirect(self):
        PasskeyLinkRequest.objects.update(used_at=timezone.now())
        client = Client()
        client.force_login(self.user)

        resp = client.get(self.url)

        self.assertTrue(resp.json()["done"])
        self.assertEqual(resp.json()["redirect"], reverse("settings-passkeys"))

    def test_another_account_cannot_see_the_status(self):
        PasskeyLinkRequest.objects.update(used_at=timezone.now())
        client = Client()
        client.force_login(self.other)

        resp = client.get(self.url)

        self.assertFalse(resp.json()["done"])
        self.assertTrue(resp.json()["expired"])


class RecentStrongSignInTest(TestCase):
    """A recent app or passkey sign-in lets enrolment skip the code, nothing else does."""

    def setUp(self):
        self.user = _user("qr-recent@example.com")
        self.factory = RequestFactory()

    def _request(self, verified, stamp):
        request = self.factory.post("/")
        request.user = self.user
        self.user.is_verified = lambda: verified
        request.session = {}
        if stamp is not None:
            request.session[STRONG_VERIFIED_KEY] = {"at": stamp, "user": self.user.pk}
        return request

    def test_a_verified_session_with_a_recent_strong_sign_in_is_allowed(self):
        request = self._request(True, timezone.now().isoformat())

        self.assertTrue(_recently_verified(request))

    def test_an_old_strong_sign_in_is_not_enough(self):
        old = (
            timezone.now() - timedelta(seconds=STRONG_VERIFIED_SECONDS + 60)
        ).isoformat()

        self.assertFalse(_recently_verified(self._request(True, old)))

    def test_a_session_that_is_not_otp_verified_is_not_enough(self):
        request = self._request(False, timezone.now().isoformat())

        self.assertFalse(_recently_verified(request))

    def test_no_stamp_means_no_recent_sign_in(self):
        self.assertFalse(_recently_verified(self._request(True, None)))

    def test_an_app_sign_in_stamps_the_session(self):
        device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )
        client = Client()
        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )

        client.post(reverse("2fa-verify"), {"token": _current_token(device)})

        self.assertIn(STRONG_VERIFIED_KEY, client.session)

    def test_an_email_sign_in_does_not_stamp_the_session(self):
        EmailMfaEnrolment.objects.create(user=self.user)
        client = Client()
        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )
        client.post(reverse("2fa-verify"), {"action": "send_email"})

        client.post(reverse("2fa-verify"), {"token": _last_code()})

        self.assertNotIn(STRONG_VERIFIED_KEY, client.session)

    def test_a_stamp_from_another_account_is_not_enough(self):
        request = self._request(True, timezone.now().isoformat())
        request.session[STRONG_VERIFIED_KEY]["user"] = self.user.pk + 1

        self.assertFalse(_recently_verified(request))

    def test_logout_clears_the_stamp(self):
        device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )
        client = Client()
        client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )
        client.post(reverse("2fa-verify"), {"token": _current_token(device)})

        client.logout()

        self.assertNotIn(STRONG_VERIFIED_KEY, client.session)

    def test_an_old_stamp_still_needs_the_password_on_the_legacy_endpoint(self):
        old = timezone.now() - timedelta(seconds=STRONG_VERIFIED_SECONDS + 60)
        client = Client()
        client.force_login(self.user)
        session = client.session
        session[STRONG_VERIFIED_KEY] = {"at": old.isoformat(), "user": self.user.pk}
        session.save()

        resp = client.post(
            reverse("passkey-register-options"),
            json.dumps({"password": "wrong", "token": "000000"}),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)

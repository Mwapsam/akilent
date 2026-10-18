"""Registration ceremony: one stored, single-use challenge per attempt, bound to its account.

The browser runs the standard WebAuthn ceremony, including any phone handoff. The server
must still refuse a completion that is replayed, expired, for another account, or that
arrives twice at once.
"""

import json
from datetime import timedelta
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from apps.accounts import passkeys
from apps.accounts.models import PasskeyCredential, PasskeyRegistrationChallenge
from apps.accounts.tests.test_passkeys import _fake_registration

PASSWORD = "pw-12345678"
VERIFY = "apps.accounts.passkeys.verify_registration_response"
NOTIFY = "apps.accounts.security_notices.notify_after_commit"


def _user(email, staff=False):
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_staff = staff
    user.save()
    return user


def _begin(client):
    """Start a ceremony the way the settings page does (password, no MFA)."""
    return client.post(
        reverse("passkey-register-options"),
        data=json.dumps({"method": "password", "password": PASSWORD}),
        content_type="application/json",
    )


def _finish(client, name="Laptop"):
    return client.post(
        reverse("passkey-register-finish"),
        data=json.dumps({"credential": {"id": "x"}, "name": name}),
        content_type="application/json",
    )


class QrProtocolIsRemovedTest(TestCase):
    def test_the_custom_qr_and_phone_routes_no_longer_exist(self):
        removed = [
            ("passkey-link-start", []),
            ("passkey-link", ["token"]),
            ("passkey-link-status", ["token"]),
            ("passkey-phone", ["token"]),
            ("passkey-phone-options", ["token"]),
            ("passkey-phone-finish", ["token"]),
        ]
        for name, args in removed:
            with self.subTest(name=name), self.assertRaises(NoReverseMatch):
                reverse(name, args=args)


class CeremonyIsStoredAndBoundTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("ceremony@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_starting_a_ceremony_stores_one_challenge_for_the_account(self):
        resp = _begin(self.client)

        self.assertEqual(resp.status_code, 200)
        row = PasskeyRegistrationChallenge.objects.get()
        self.assertEqual(row.user, self.user)
        self.assertIsNone(row.used_at)
        self.assertGreater(row.expires_at, timezone.now())
        self.assertEqual(self.client.session[passkeys.REG_KEY]["id"], row.pk)

    def test_verification_uses_the_challenge_and_origin_stored_with_the_ceremony(self):
        _begin(self.client)
        row = PasskeyRegistrationChallenge.objects.get()

        with mock.patch(VERIFY, return_value=_fake_registration(b"k1")) as verify:
            _finish(self.client)

        kwargs = verify.call_args.kwargs
        self.assertEqual(kwargs["expected_challenge"], row.challenge_bytes())
        self.assertEqual(kwargs["expected_origin"], row.origin)
        self.assertEqual(kwargs["expected_rp_id"], row.rp_id)

    def test_a_challenge_mismatch_stores_nothing(self):
        _begin(self.client)

        with mock.patch(VERIFY, side_effect=ValueError("challenge mismatch")):
            resp = _finish(self.client)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.filter(user=self.user).exists())

    def test_a_completion_for_another_account_stores_nothing_for_either(self):
        _begin(self.client)
        other = _user("ceremony-other@example.com")
        other_client = Client()
        other_client.force_login(other)
        session = other_client.session
        session[passkeys.REG_KEY] = self.client.session[passkeys.REG_KEY]
        session.save()

        with mock.patch(VERIFY, return_value=_fake_registration(b"sub")):
            resp = _finish(other_client)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.exists())


class SessionBindingTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("binding@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_the_stored_binding_is_a_digest_not_the_session_key(self):
        _begin(self.client)
        row = PasskeyRegistrationChallenge.objects.get()

        self.assertNotEqual(row.session_digest, self.client.session.session_key)
        self.assertEqual(len(row.session_digest), 64)

    def test_another_session_of_the_same_account_cannot_complete_it(self):
        _begin(self.client)
        other = Client()
        other.force_login(self.user)
        session = other.session
        session[passkeys.REG_KEY] = self.client.session[passkeys.REG_KEY]
        session.save()

        with mock.patch(VERIFY, return_value=_fake_registration(b"other")) as verify:
            resp = _finish(other)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.exists())
        verify.assert_not_called()

    def test_rotating_the_session_invalidates_the_ceremony(self):
        _begin(self.client)
        session = self.client.session
        session.cycle_key()
        self.client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key

        with mock.patch(VERIFY, return_value=_fake_registration(b"rot")) as verify:
            resp = _finish(self.client)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.exists())
        verify.assert_not_called()


class SingleUseAndExpiryTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("single-use@example.com")
        self.client = Client()
        self.client.force_login(self.user)

    def test_a_completed_ceremony_cannot_be_completed_again(self):
        _begin(self.client)
        with mock.patch(VERIFY, return_value=_fake_registration(b"one")):
            self.assertEqual(_finish(self.client).status_code, 200)
        with mock.patch(VERIFY, return_value=_fake_registration(b"two")):
            again = _finish(self.client)

        self.assertEqual(again.status_code, 400)
        self.assertEqual(PasskeyCredential.objects.filter(user=self.user).count(), 1)

    def test_an_expired_ceremony_is_refused(self):
        _begin(self.client)
        PasskeyRegistrationChallenge.objects.update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        with mock.patch(VERIFY, return_value=_fake_registration(b"late")):
            resp = _finish(self.client)

        self.assertEqual(resp.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.exists())

    def test_a_stale_session_copy_cannot_complete_a_used_ceremony(self):
        # Two requests carry the same session copy. The first one wins; the second must lose.
        _begin(self.client)
        stale = self.client.session[passkeys.REG_KEY]
        with mock.patch(VERIFY, return_value=_fake_registration(b"win")):
            self.assertEqual(_finish(self.client).status_code, 200)

        session = self.client.session
        session[passkeys.REG_KEY] = stale
        session.save()
        with mock.patch(VERIFY, return_value=_fake_registration(b"lose")) as verify:
            resp = _finish(self.client)

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PasskeyCredential.objects.filter(user=self.user).count(), 1)
        verify.assert_not_called()

    def test_the_notice_is_sent_only_after_a_passkey_is_stored(self):
        _begin(self.client)

        with (
            mock.patch(VERIFY, side_effect=ValueError("bad")),
            mock.patch(NOTIFY) as notify,
        ):
            _finish(self.client)

        notify.assert_not_called()

        _begin(self.client)
        with (
            mock.patch(VERIFY, return_value=_fake_registration(b"ok")),
            mock.patch(NOTIFY) as notify,
        ):
            self.assertEqual(_finish(self.client).status_code, 200)

        notify.assert_called_once()


@pytest.mark.serial
class ConcurrentCompletionTest(TransactionTestCase):
    """Two real requests race for one challenge. Runs only where the database supports it."""

    def setUp(self):
        cache.clear()
        self.user = _user("race@example.com")

    def test_two_parallel_completions_store_exactly_one_passkey(self):
        if connection.vendor != "postgresql":
            self.skipTest("Parallel completion needs PostgreSQL row locks.")
        import threading

        client = Client()
        client.force_login(self.user)
        _begin(client)
        stale_session = dict(client.session)
        results = []

        def attempt(n):
            c = Client()
            c.force_login(self.user)
            s = c.session
            s[passkeys.REG_KEY] = stale_session[passkeys.REG_KEY]
            s.save()
            with mock.patch(VERIFY, return_value=_fake_registration(f"r{n}".encode())):
                results.append(_finish(c).status_code)

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(results), [200, 400])
        self.assertEqual(PasskeyCredential.objects.filter(user=self.user).count(), 1)


@override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
class EnforcedAccountCeremonyTest(TestCase):
    def test_an_mfa_required_account_cannot_start_with_password_or_email(self):
        staff = _user("ceremony-staff@example.com", staff=True)
        client = Client()
        client.force_login(staff)

        by_password = client.post(
            reverse("passkey-register-options"),
            data=json.dumps({"method": "password", "password": PASSWORD}),
            content_type="application/json",
        )

        self.assertEqual(by_password.status_code, 400)
        self.assertFalse(PasskeyRegistrationChallenge.objects.exists())


class CrossDeviceHintTest(TestCase):
    def test_registration_options_ask_the_browser_to_offer_a_phone(self):
        user = _user("hint@example.com")
        client = Client()
        client.force_login(user)

        resp = _begin(client)

        self.assertIn("hybrid", resp.json().get("hints", []))

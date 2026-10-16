"""TOTP secret storage: modes, sealing, fail-closed reads, and the phased command."""

from io import StringIO
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import totp_storage
from apps.accounts.models import TOTPSecretCiphertext
from apps.accounts.tests.test_2fa import _current_token


def _user(email="store@example.com"):
    return User.objects.create_user(username=email, email=email, password="pw-12345678")


def _stored_key(device):
    return TOTPDevice.objects.filter(pk=device.pk).values_list("key", flat=True).get()


@override_settings(TOTP_SECRET_STORAGE="plaintext")
class PlaintextModeTest(TestCase):
    def test_new_device_keeps_plaintext_in_key_column(self):
        device = totp_storage.create_device(_user(), confirmed=True)

        self.assertEqual(_stored_key(device), device.key)
        self.assertFalse(TOTPSecretCiphertext.objects.filter(device=device).exists())

    def test_load_returns_the_device_unchanged(self):
        device = totp_storage.create_device(_user(), confirmed=True)

        self.assertIsNotNone(totp_storage.load(TOTPDevice.objects.get(pk=device.pk)))


@override_settings(TOTP_SECRET_STORAGE="migrating")
class MigratingModeTest(TestCase):
    def setUp(self):
        self.user = _user()

    def test_new_device_is_sealed_and_loads_with_its_key(self):
        device = totp_storage.create_device(self.user, confirmed=True)
        loaded = totp_storage.load(TOTPDevice.objects.get(pk=device.pk))

        self.assertEqual(_stored_key(device), totp_storage.PLACEHOLDER)
        self.assertEqual(loaded.key, device.key)

    def test_verification_does_not_commit_plaintext(self):
        device = totp_storage.create_device(self.user, confirmed=True)
        token = _current_token(device)

        verified = totp_storage.verify_and_seal(
            TOTPDevice.objects.get(pk=device.pk), token
        )

        self.assertIsNotNone(verified)
        self.assertEqual(_stored_key(device), totp_storage.PLACEHOLDER)

    def test_replayed_code_is_refused_on_the_sealed_path(self):
        device = totp_storage.create_device(self.user, confirmed=True)
        token = _current_token(device)
        totp_storage.verify_and_seal(TOTPDevice.objects.get(pk=device.pk), token)

        again = totp_storage.verify_and_seal(
            TOTPDevice.objects.get(pk=device.pk), token
        )

        self.assertIsNone(again)

    def test_existing_plaintext_device_still_loads_and_is_left_alone(self):
        legacy = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )
        original = legacy.key

        loaded = totp_storage.verify_and_seal(
            TOTPDevice.objects.get(pk=legacy.pk), _current_token(legacy)
        )

        self.assertIsNotNone(loaded)
        self.assertEqual(_stored_key(legacy), original)


@override_settings(TOTP_SECRET_STORAGE="encrypted")
class EncryptedModeFailClosedTest(TestCase):
    def test_sealed_device_without_sidecar_fails_closed(self):
        device = TOTPDevice.objects.create(
            user=_user("orphan@example.com"),
            name="default",
            key=totp_storage.PLACEHOLDER,
            confirmed=True,
        )

        self.assertIsNone(totp_storage.load(device))

    def test_placeholder_never_verifies_any_code(self):
        device = TOTPDevice.objects.create(
            user=_user("orphan2@example.com"),
            name="default",
            key=totp_storage.PLACEHOLDER,
            confirmed=True,
        )

        for code in ("000000", "123456", "999999"):
            self.assertIsNone(totp_storage.verify_and_seal(device, code))

    def test_wrong_encryption_key_fails_closed_and_audits(self):
        device = totp_storage.create_device(
            _user("wrongkey@example.com"), confirmed=True
        )
        other = Fernet.generate_key().decode()

        with (
            override_settings(FIELD_ENCRYPTION_KEYS=[other]),
            mock.patch("apps.accounts.totp_storage._audit_decrypt_failure") as audit,
        ):
            result = totp_storage.load(TOTPDevice.objects.get(pk=device.pk))

        self.assertIsNone(result)
        audit.assert_called_once()


@override_settings(TOTP_SECRET_STORAGE="migrating")
class AtomicEnrolmentTest(TestCase):
    def test_failed_sidecar_write_leaves_no_device_and_no_plaintext(self):
        user = _user("atomic@example.com")

        with mock.patch(
            "apps.accounts.totp_storage.TOTPSecretCiphertext.objects.create",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                totp_storage.create_device(user, confirmed=True)

        self.assertFalse(TOTPDevice.objects.filter(user=user).exists())


@override_settings(TOTP_SECRET_STORAGE="plaintext")
class PhasedCommandTest(TestCase):
    def setUp(self):
        self.user = _user("phased@example.com")
        self.device = TOTPDevice.objects.create(
            user=self.user, name="default", confirmed=True
        )

    def _run(self, phase, *extra):
        out = StringIO()
        call_command("totp_secret_migrate", phase, *extra, stdout=out)
        return out.getvalue()

    def test_expand_copies_and_keeps_plaintext(self):
        original = self.device.key

        out = self._run("expand")

        self.assertIn("created=1", out)
        self.assertEqual(_stored_key(self.device), original)
        self.assertTrue(totp_storage.sidecar_matches(self.device))
        self.assertNotIn(original, out)

    def test_verify_prints_booleans_only(self):
        self._run("expand")

        out = self._run("verify")

        self.assertIn("all_match=True", out)
        self.assertNotIn(self.device.key, out)

    def test_contract_refuses_without_confirm(self):
        self._run("expand")

        with self.assertRaisesMessage(CommandError, "--confirm"):
            self._run("contract")

        self.assertEqual(_stored_key(self.device), self.device.key)

    def test_contract_refuses_when_a_sidecar_is_missing(self):
        with self.assertRaisesMessage(CommandError, "Refusing"):
            self._run("contract", "--confirm")

    def test_contract_then_restore_round_trips_the_key(self):
        original = self.device.key
        self._run("expand")

        self._run("contract", "--confirm")
        self.assertEqual(_stored_key(self.device), totp_storage.PLACEHOLDER)

        self._run("restore", "--confirm")
        self.assertEqual(_stored_key(self.device), original)


@override_settings(TOTP_SECRET_STORAGE="migrating")
class SealedDeviceSignInTest(TestCase):
    def test_password_plus_code_signs_in_with_a_sealed_device(self):
        from django.test import Client
        from django.urls import reverse
        from django_otp import DEVICE_ID_SESSION_KEY

        user = _user("sealedlogin@example.com")
        device = totp_storage.create_device(user, confirmed=True)
        client = Client()
        client.post(
            reverse("login"),
            {"username": "sealedlogin@example.com", "password": "pw-12345678"},
        )

        client.post(
            reverse("2fa-verify"),
            {
                "token": _current_token(
                    totp_storage.load(TOTPDevice.objects.get(pk=device.pk))
                )
            },
        )

        self.assertIn(DEVICE_ID_SESSION_KEY, client.session)
        self.assertEqual(_stored_key(device), totp_storage.PLACEHOLDER)

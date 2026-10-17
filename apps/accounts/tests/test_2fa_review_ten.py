"""Regression tests for the ten findings in the second 2FA review."""

from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts import recovery, views
from apps.accounts.models import EmailMfaEnrolment, PasskeyCredential
from apps.accounts.security_notices import TWO_FACTOR_DISABLED, _send
from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_2fa_hardening import _enrolled
from apps.core.models import PlatformAuditLog

PASSWORD = "pw-12345678"


def _user(email):
    return User.objects.create_user(username=email, email=email, password=PASSWORD)


class Finding2PasswordLimiterIsAtomicTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("limiter@example.com")

    def test_a_correct_password_is_refused_once_the_limit_is_already_used(self):
        # Simulate the parallel requests: the limit's worth of attempts is already counted.
        cache.set(
            f"reauth-password-failures:{self.user.pk}",
            views.REAUTH_PASSWORD_LIMIT,
            views.REAUTH_PASSWORD_WINDOW_SECONDS,
        )

        with mock.patch.object(User, "check_password", return_value=True) as check:
            ok, wait = views._reauth_outcome(self.user, PASSWORD, "000000")

        self.assertFalse(ok)
        self.assertIn("Too many failed attempts", wait)
        check.assert_not_called()

    def test_each_attempt_is_counted_before_its_password_is_checked(self):
        with mock.patch.object(User, "check_password", return_value=False):
            for _ in range(views.REAUTH_PASSWORD_LIMIT):
                views._reauth_outcome(self.user, "wrong", "000000")
            ok, wait = views._reauth_outcome(self.user, "wrong", "000000")

        self.assertFalse(ok)
        self.assertIn("Too many failed attempts", wait)


class Finding3LockedSignInAttemptsAreAudited(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("locked-audit@example.com")
        self.device = _enrolled(self.user)
        self.device.throttling_failure_count = 50
        self.device.throttling_failure_timestamp = timezone.now()
        self.device.save()
        self.client = Client()
        self.client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )

    def test_a_locked_app_attempt_is_recorded(self):
        self.client.post(reverse("2fa-verify"), {"token": "000000"})

        row = (
            PlatformAuditLog.objects.filter(action="auth.2fa_failed")
            .order_by("pk")
            .last()
        )
        self.assertIsNotNone(row)
        self.assertTrue(row.metadata.get("locked"))


class Finding4NoticeForEmailCodeUsers(TestCase):
    def setUp(self):
        self.user = _user("notice-email@example.com")
        self.patcher = mock.patch("apps.email.services.send.send_system_email")
        self.send = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_email_code_users_are_not_told_the_password_is_the_only_way(self):
        EmailMfaEnrolment.objects.create(user=self.user)

        _send(self.user.pk, self.user.email, TWO_FACTOR_DISABLED)

        body = self.send.call_args.kwargs["text_body"]
        self.assertIn("email sign-in codes are now the only ways", body)
        self.assertNotIn("only way to sign in", body)


class Finding5PasskeyMessageIsShown(TestCase):
    def test_the_passkey_script_reads_the_server_message(self):
        script = Path(settings.BASE_DIR, "static/js/passkeys.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("body.error", script)


class Finding6RegenerationFailureReturnsToTheFormPage(TestCase):
    def setUp(self):
        self.user = _user("regen-redirect@example.com")
        self.device = _enrolled(self.user)
        recovery.generate_codes(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_a_wrong_password_on_regenerate_lands_on_the_setup_page(self):
        resp = self.client.post(
            reverse("recovery-codes-regenerate"),
            {"password": "wrong", "token": _current_token(self.device)},
        )

        self.assertRedirects(resp, reverse("2fa-setup"), fetch_redirect_response=False)


class Finding7PasskeyChangesRefuseRecoveryCodes(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("passkey-recovery@example.com")
        self.device = _enrolled(self.user)
        self.codes = recovery.generate_codes(self.user)
        self.passkey = PasskeyCredential.objects.create(
            user=self.user,
            name="Key",
            credential_id=b"rc",
            public_key=b"p",
            confirmed=True,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_a_recovery_code_does_not_remove_a_passkey(self):
        self.client.post(
            reverse("passkey-remove", args=[self.passkey.pk]),
            {"password": PASSWORD, "token": self.codes[0]},
        )

        self.assertTrue(PasskeyCredential.objects.filter(pk=self.passkey.pk).exists())
        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT)

    def test_the_app_code_still_removes_a_passkey(self):
        self.client.post(
            reverse("passkey-remove", args=[self.passkey.pk]),
            {"password": PASSWORD, "token": _current_token(self.device)},
        )

        self.assertFalse(PasskeyCredential.objects.filter(pk=self.passkey.pk).exists())


class Finding8NoDeadReauthHelper(TestCase):
    def test_the_generic_helper_is_gone(self):
        self.assertFalse(hasattr(views, "_reauthenticate"))


class Finding10PendingUserSkipsTheDevice(TestCase):
    def test_pending_user_does_not_look_up_the_device(self):
        user = _user("pending-user@example.com")
        _enrolled(user)
        request = RequestFactory().get("/")
        request.session = {views._2FA_SESSION_KEY: user.pk}

        pending_user, device = views._pending_second_factor(request, with_device=False)

        self.assertEqual(pending_user, user)
        self.assertIsNone(device)

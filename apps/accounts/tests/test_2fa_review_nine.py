"""Regression tests for the nine findings in the uncommitted 2FA review."""

from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts import recovery
from apps.accounts.models import PasskeyCredential
from apps.accounts.security_notices import TWO_FACTOR_DISABLED, _send
from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_2fa_hardening import _enrolled
from apps.accounts.views import _wait_text
from apps.core.models import PlatformAuditLog

PASSWORD = "pw-12345678"


def _user(email, staff=False):
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_staff = staff
    user.save()
    return user


class Finding1LockoutCopyTest(TestCase):
    def test_a_long_lockout_does_not_suggest_a_recovery_code(self):
        info = {"locked_until": timezone.now() + timedelta(hours=2)}

        message = _wait_text(info)

        self.assertEqual(message, "Too many attempts. Try again later.")
        self.assertNotIn("recovery", message)


class Finding2EnableAppKeepsCodesReachableTest(TestCase):
    @override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
    def test_an_enforced_user_who_enables_an_app_sees_the_codes(self):
        user = _user("enable-enforced@example.com", staff=True)
        client = Client()
        client.force_login(user)
        client.get(reverse("2fa-setup"))
        device = TOTPDevice.objects.get(user=user, confirmed=False)

        resp = client.post(
            reverse("2fa-setup"),
            {"action": "enable", "token": _current_token(device)},
        )

        self.assertRedirects(
            resp, reverse("settings-recovery-codes"), fetch_redirect_response=False
        )
        page = client.get(reverse("settings-recovery-codes"))
        self.assertEqual(len(page.context["codes"]), recovery.CODE_COUNT)
        self.assertEqual(page.status_code, 200)


class Finding3NoticeMatchesPasskeysTest(TestCase):
    def setUp(self):
        self.user = _user("notice-keys@example.com")
        self.patcher = mock.patch("apps.email.services.send.send_system_email")
        self.send = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_a_passkey_user_is_not_told_the_password_is_the_only_way(self):
        PasskeyCredential.objects.create(
            user=self.user,
            name="Key",
            credential_id=b"n1",
            public_key=b"p",
            confirmed=True,
        )

        _send(self.user.pk, self.user.email, TWO_FACTOR_DISABLED)

        body = self.send.call_args.kwargs["text_body"]
        self.assertIn("passkeys and security keys still work", body)
        self.assertNotIn("only way to sign in", body)

    def test_an_account_without_passkeys_keeps_the_password_wording(self):
        _send(self.user.pk, self.user.email, TWO_FACTOR_DISABLED)

        body = self.send.call_args.kwargs["text_body"]
        self.assertIn("only way to sign in", body)

    def test_the_confirm_text_no_longer_claims_password_only(self):
        text = Path(
            settings.BASE_DIR, "templates/accounts/settings_2fa.html"
        ).read_text(encoding="utf-8")

        self.assertNotIn("Your password will be the only protection", text)


class Finding4LockedAttemptsAreAuditedTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("audit-lock@example.com")
        self.device = _enrolled(self.user)
        self.codes = recovery.generate_codes(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_locked_disable_attempts_are_recorded(self):
        for _ in range(6):
            self.client.post(
                reverse("2fa-setup"),
                {"action": "disable", "password": "wrong", "token": "000000"},
            )

        rows = PlatformAuditLog.objects.filter(
            action="auth.2fa_disable_failed"
        ).order_by("pk")
        self.assertEqual(rows.count(), 6)
        self.assertTrue(rows.last().metadata.get("locked"))

    def test_failed_regeneration_is_recorded(self):
        self.client.post(
            reverse("recovery-codes-regenerate"),
            {"password": "wrong", "token": "000000"},
        )

        self.assertTrue(
            PlatformAuditLog.objects.filter(
                action="auth.recovery_codes_regen_failed"
            ).exists()
        )


class Finding5PasskeyLockoutMessageTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _user("passkey-lock@example.com")
        self.device = _enrolled(self.user)
        self.passkey = PasskeyCredential.objects.create(
            user=self.user,
            name="Key",
            credential_id=b"pl",
            public_key=b"p",
            confirmed=True,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_a_locked_removal_says_so(self):
        url = reverse("passkey-remove", args=[self.passkey.pk])
        for _ in range(5):
            self.client.post(url, {"password": "wrong", "token": "000000"})

        resp = self.client.post(
            url,
            {"password": PASSWORD, "token": _current_token(self.device)},
            follow=True,
        )

        self.assertContains(resp, "Too many failed attempts")
        self.assertTrue(PasskeyCredential.objects.filter(pk=self.passkey.pk).exists())


class Finding6EnabledPageShowsMessagesTest(TestCase):
    def test_a_queued_message_renders_on_the_enabled_page(self):
        from django.template.loader import render_to_string

        request = RequestFactory().get("/settings/security/2fa/")
        request.session = {}
        request._messages = FallbackStorage(request)
        from django.contrib import messages

        messages.success(request, "Two-factor authentication disabled.")

        html = render_to_string(
            "accounts/settings_2fa.html",
            {"enabled": True, "account": None},
            request=request,
        )

        self.assertIn("Two-factor authentication disabled.", html)


class Finding7NoInlineFocusScriptsTest(TestCase):
    def test_the_sign_in_templates_rely_on_the_shared_focus_handling(self):
        for rel in [
            "templates/auth/2fa_verify.html",
            "templates/auth/2fa_recovery.html",
            "templates/auth/password_reset_confirm.html",
        ]:
            text = Path(settings.BASE_DIR, rel).read_text(encoding="utf-8")
            self.assertNotIn("setTimeout(function () { var", text, rel)


class Finding9SharedTouchTargetTest(TestCase):
    def test_the_touch_target_rule_is_one_class_not_repeated_inline_styles(self):
        css = Path(settings.BASE_DIR, "assets/app.css").read_text(encoding="utf-8")
        self.assertIn(".touch-target", css)
        for rel in [
            "templates/accounts/settings_2fa.html",
            "templates/accounts/settings_email_mfa.html",
            "templates/accounts/settings_passkeys.html",
            "templates/accounts/_recovery_regenerate_form.html",
            "templates/components/send_menu.html",
        ]:
            text = Path(settings.BASE_DIR, rel).read_text(encoding="utf-8")
            self.assertNotIn("min-height:44px;min-width:44px", text, rel)

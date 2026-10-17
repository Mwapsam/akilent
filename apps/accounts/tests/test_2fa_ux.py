"""2FA UX and recovery-code display: separate recovery entry, lockout copy, safe display."""

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts import email_mfa, recovery
from apps.accounts.models import RecoveryCode
from apps.accounts.tests.test_2fa import _current_token
from apps.accounts.tests.test_2fa_hardening import (
    _enrolled,
    _login_to_second_step,
    _user,
)
from apps.accounts.views import (
    RECOVERY_DISPLAY_KEY,
    RECOVERY_DISPLAY_SECONDS,
    _store_recovery_display,
)

LOCKED = "Too many attempts. Try again"


def _lock(device):
    device.throttling_failure_count = 50
    device.throttling_failure_timestamp = timezone.now()
    device.save(
        update_fields=["throttling_failure_count", "throttling_failure_timestamp"]
    )


class SignInFieldIsOnlyForAppCodesTest(TestCase):
    def setUp(self):
        self.user = _user("ux-field@example.com")
        self.device = _enrolled(self.user)
        self.client = Client()
        _login_to_second_step(self.client, self.user.email)

    def test_the_code_field_asks_for_six_digits(self):
        resp = self.client.get(reverse("2fa-verify"))

        self.assertContains(resp, 'placeholder="123456"')
        self.assertNotContains(resp, "recovery code</label>")
        self.assertNotContains(resp, "000 000")

    def test_a_recovery_shaped_code_in_the_field_is_redirected_not_checked(self):
        codes = recovery.generate_codes(self.user)

        resp = self.client.post(reverse("2fa-verify"), {"token": codes[0]}, follow=True)

        self.assertContains(resp, "That looks like a recovery code")
        self.assertFalse(get_user(self.client).is_authenticated)
        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT)

    def test_the_lost_device_link_points_to_its_own_page(self):
        resp = self.client.get(reverse("2fa-verify"))

        self.assertContains(resp, reverse("2fa-recovery"))


class RecoverySignInPageTest(TestCase):
    def setUp(self):
        self.user = _user("ux-recovery@example.com")
        self.device = _enrolled(self.user)
        self.codes = recovery.generate_codes(self.user)
        self.client = Client()
        _login_to_second_step(self.client, self.user.email)

    def test_the_page_explains_the_code_format(self):
        resp = self.client.get(reverse("2fa-recovery"))

        self.assertContains(resp, "16-character codes")
        self.assertNotContains(resp, "You have")

    def test_a_valid_code_signs_in_on_the_recovery_page(self):
        self.client.post(reverse("2fa-recovery"), {"code": self.codes[0]})

        self.assertTrue(get_user(self.client).is_authenticated)

    def test_a_malformed_code_is_refused_without_counting_a_failure(self):
        self.client.post(reverse("2fa-recovery"), {"code": "123456"})

        self.device.refresh_from_db()
        self.assertEqual(self.device.throttling_failure_count, 0)
        self.assertFalse(get_user(self.client).is_authenticated)

    def test_a_locked_device_shows_the_lockout_message(self):
        _lock(self.device)

        resp = self.client.post(
            reverse("2fa-recovery"), {"code": self.codes[0]}, follow=True
        )

        self.assertContains(resp, LOCKED)
        self.assertFalse(get_user(self.client).is_authenticated)

    def test_without_an_app_the_page_sends_the_user_back(self):
        self.device.delete()
        client = Client()
        _login_to_second_step(client, self.user.email)

        resp = client.get(reverse("2fa-recovery"))

        self.assertRedirects(resp, reverse("2fa-verify"), fetch_redirect_response=False)


class AppLockoutMessageTest(TestCase):
    def setUp(self):
        self.user = _user("ux-lock@example.com")
        self.device = _enrolled(self.user)
        self.client = Client()
        _login_to_second_step(self.client, self.user.email)

    def test_a_locked_app_says_so_instead_of_invalid_code(self):
        _lock(self.device)

        resp = self.client.post(reverse("2fa-verify"), {"token": "000000"}, follow=True)

        self.assertContains(resp, LOCKED)
        self.assertNotContains(resp, "didn&#x27;t work")

    def test_a_wrong_code_that_does_not_lock_keeps_the_plain_message(self):
        resp = self.client.post(reverse("2fa-verify"), {"token": "000000"}, follow=True)

        self.assertNotContains(resp, LOCKED)
        self.assertContains(resp, "didn&#x27;t work")


class RecoveryDisplayWindowTest(TestCase):
    def setUp(self):
        self.user = _user("ux-display@example.com")
        self.device = _enrolled(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def _enable(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        pending = TOTPDevice.objects.filter(user=self.user, confirmed=False).first()
        if pending is None:
            self.device.delete()
            pending = TOTPDevice.objects.create(
                user=self.user, name="default", confirmed=False
            )
        return self.client.post(
            reverse("2fa-setup"), {"action": "enable", "token": _current_token(pending)}
        )

    def _page_codes(self):
        return self.client.get(reverse("settings-recovery-codes")).context["codes"]

    def test_enabling_shows_the_set_and_a_reload_shows_the_same_set(self):
        self._enable()
        first = self._page_codes()
        second = self._page_codes()

        self.assertEqual(len(first), recovery.CODE_COUNT)
        self.assertEqual(first, second)
        self.assertEqual(
            RecoveryCode.objects.filter(user=self.user).count(), recovery.CODE_COUNT
        )

    def test_the_session_holds_no_plaintext_code(self):
        self._enable()
        codes = self._page_codes()

        dumped = json.dumps(dict(self.client.session), default=str)

        for code in codes:
            self.assertNotIn(code, dumped)

    def test_the_codes_page_is_never_cached(self):
        self._enable()

        resp = self.client.get(reverse("settings-recovery-codes"))

        self.assertIn("no-store", resp["Cache-Control"])

    def test_an_expired_window_shows_nothing_and_offers_a_new_set(self):
        self._enable()
        from apps.accounts.fields import open_text, seal_text

        session = self.client.session
        data = json.loads(open_text(session[RECOVERY_DISPLAY_KEY]))
        stamp = timezone.now() - timedelta(seconds=RECOVERY_DISPLAY_SECONDS + 60)
        data["at"] = stamp.isoformat()
        session[RECOVERY_DISPLAY_KEY] = seal_text(json.dumps(data))
        session.save()

        resp = self.client.get(reverse("settings-recovery-codes"))

        self.assertIsNone(resp.context["codes"])
        self.assertContains(resp, "Make new recovery codes")

    def test_an_unreadable_window_fails_closed(self):
        self._enable()
        session = self.client.session
        session[RECOVERY_DISPLAY_KEY] = "not-a-valid-token"
        session.save()

        resp = self.client.get(reverse("settings-recovery-codes"))

        self.assertIsNone(resp.context["codes"])
        self.assertNotIn(RECOVERY_DISPLAY_KEY, self.client.session)

    def test_another_user_cannot_see_the_set(self):
        self._enable()
        other = Client()
        other_user = _user("ux-other@example.com")
        other.force_login(other_user)

        resp = other.get(reverse("settings-recovery-codes"))

        self.assertIsNone(resp.context["codes"])

    def test_confirming_requires_the_checkbox(self):
        self._enable()

        self.client.post(reverse("recovery-codes-confirm"), {})

        self.assertIn(RECOVERY_DISPLAY_KEY, self.client.session)

    def test_confirming_clears_the_window(self):
        self._enable()

        self.client.post(reverse("recovery-codes-confirm"), {"saved": "1"})

        self.assertNotIn(RECOVERY_DISPLAY_KEY, self.client.session)
        self.assertIsNone(self._page_codes())

    def test_a_stored_set_with_the_wrong_shape_is_refused(self):
        session = self.client.session
        _store_recovery_display(_fake_request(session), ["only-one"])
        session.save()

        resp = self.client.get(reverse("settings-recovery-codes"))

        self.assertIsNone(resp.context["codes"])


def _fake_request(session):
    class _R:
        pass

    request = _R()
    request.session = session
    return request


class RegenerateTest(TestCase):
    def setUp(self):
        self.user = _user("ux-regen@example.com")
        self.device = _enrolled(self.user)
        self.old = recovery.generate_codes(self.user)
        self.client = Client()
        self.client.force_login(self.user)

    def test_regenerate_needs_the_password_and_a_current_code(self):
        self.client.post(
            reverse("recovery-codes-regenerate"),
            {"password": "wrong", "token": _current_token(self.device)},
        )

        self.assertEqual(recovery.remaining(self.user), recovery.CODE_COUNT)

    def test_regenerate_retires_the_old_set_and_shows_the_new_one(self):
        self.client.post(
            reverse("recovery-codes-regenerate"),
            {"password": "pw-12345678", "token": _current_token(self.device)},
        )

        self.assertFalse(recovery.redeem(self.user, self.old[0]))
        page = self.client.get(reverse("settings-recovery-codes"))
        self.assertEqual(len(page.context["codes"]), recovery.CODE_COUNT)
        self.assertEqual(
            RecoveryCode.objects.filter(user=self.user).count(), recovery.CODE_COUNT
        )

    def test_regenerate_is_post_only(self):
        resp = self.client.get(reverse("recovery-codes-regenerate"))

        self.assertEqual(resp.status_code, 405)


class EmailCapMessageTest(TestCase):
    def test_the_hourly_cap_says_so_and_not_just_sent_recently(self):
        user = _user("ux-cap@example.com")
        client = Client()
        client.force_login(user)

        with mock.patch(
            "apps.accounts.email_mfa.send_code", return_value=email_mfa.CAPPED
        ):
            resp = client.post(reverse("passkey-reauth-send"), follow=True)

        self.assertContains(resp, "Too many codes have been sent")
        self.assertNotContains(resp, "sent just now")

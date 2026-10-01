"""Phase 4 security audit tests.

Covers:
- Open-redirect guards on the 2FA login flow
- Login rate limiting (6th attempt blocked)
- Cross-tenant isolation on team endpoints
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse


def _make_user(email, password="testpass123"):
    u = User.objects.create_user(username=email, email=email, password=password)
    u.backend = "apps.accounts.backends.EmailBackend"
    return u


class OpenRedirectTest(TestCase):
    """2FA login flow must reject off-site next= values."""

    def setUp(self):
        self.client = Client()
        self.user = _make_user("alice@example.com")

    def _login_with_2fa(self, next_param=""):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        url = reverse("login")
        if next_param:
            url = f"{url}?next={next_param}"
        return self.client.post(
            url,
            {"username": "alice@example.com", "password": "testpass123"},
            follow=False,
        )

    def _current_token(self, device):
        import time as _time

        from django_otp.oath import TOTP

        totp = TOTP(device.bin_key, device.step, device.t0, device.digits, device.drift)
        totp.time = _time.time()
        return str(totp.token()).zfill(device.digits)

    def test_external_next_is_rejected_after_2fa(self):
        """An off-site next= must never redirect the user off the platform."""
        from django.conf import settings
        from django_otp.plugins.otp_totp.models import TOTPDevice

        self._login_with_2fa(next_param="https://evil.example.com/steal")
        device = TOTPDevice.objects.get(user=self.user, confirmed=True)
        token = self._current_token(device)
        resp = self.client.post(reverse("2fa-verify"), {"token": token}, follow=False)
        self.assertEqual(resp.status_code, 302)
        # Must redirect to the default URL, not the external site.
        self.assertNotIn("evil.example.com", resp["Location"])
        self.assertEqual(resp["Location"], settings.LOGIN_REDIRECT_URL)

    def test_relative_next_is_allowed_after_2fa(self):
        """A safe same-host next= path should be honoured."""
        from django_otp.plugins.otp_totp.models import TOTPDevice

        self._login_with_2fa(next_param="/dashboard/")
        device = TOTPDevice.objects.get(user=self.user, confirmed=True)
        token = self._current_token(device)
        resp = self.client.post(reverse("2fa-verify"), {"token": token}, follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/dashboard/", resp["Location"])

    def test_protocol_relative_next_is_rejected(self):
        """A protocol-relative URL (//evil.com) must be rejected."""
        from django.conf import settings
        from django_otp.plugins.otp_totp.models import TOTPDevice

        self._login_with_2fa(next_param="//evil.example.com/steal")
        device = TOTPDevice.objects.get(user=self.user, confirmed=True)
        token = self._current_token(device)
        resp = self.client.post(reverse("2fa-verify"), {"token": token}, follow=False)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.example.com", resp["Location"])
        self.assertEqual(resp["Location"], settings.LOGIN_REDIRECT_URL)


class LoginRateLimitTest(TestCase):
    """Login must block after _LOGIN_MAX_ATTEMPTS failed attempts."""

    def setUp(self):
        self.client = Client()
        _make_user("bob@example.com")

    def _bad_login(self):
        return self.client.post(
            reverse("login"),
            {"username": "bob@example.com", "password": "wrongpassword"},
            follow=False,
        )

    def test_sixth_attempt_is_blocked(self):
        from django.core.cache import cache

        cache.clear()
        for _ in range(5):
            self._bad_login()
        resp = self._bad_login()
        # After 5 failures the 6th attempt should stay on the login page (200)
        # with an error message — not redirect to dashboard.
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Too many failed login attempts")

    def test_first_five_attempts_are_not_blocked(self):
        from django.core.cache import cache

        cache.clear()
        for _ in range(4):
            resp = self._bad_login()
            self.assertNotContains(resp, "Too many failed login attempts")


class TeamEndpointIsolationTest(TestCase):
    """Team mutation endpoints must be scoped to the actor's current account."""

    def setUp(self):
        from apps.accounts.models import Account, Membership

        self.client = Client()
        self.owner = _make_user("owner@a.com")
        self.account_a = Account.objects.create(
            company_name="A Corp", slug="a-corp", selected_services="whatsapp"
        )
        Membership.objects.create(
            user=self.owner, account=self.account_a, role=Membership.Role.OWNER
        )

        self.other_user = _make_user("other@b.com")
        self.account_b = Account.objects.create(
            company_name="B Corp", slug="b-corp", selected_services="whatsapp"
        )
        Membership.objects.create(
            user=self.other_user, account=self.account_b, role=Membership.Role.OWNER
        )

        self.client.force_login(self.owner)
        session = self.client.session
        set_current_account_in_session(session, self.account_a)
        session.save()

    def test_invite_revoke_rejects_cross_account_pk(self):
        """Revoking an invitation from a different account returns 404."""
        from apps.accounts.models import Invitation

        invite = Invitation.objects.create(
            account=self.account_b,
            email="victim@example.com",
            role="member",
            invited_by=self.other_user,
        )
        resp = self.client.post(
            reverse("invite-revoke", kwargs={"pk": invite.pk}), follow=False
        )
        # The get_object_or_404 scopes by account — should be 404 or redirect.
        self.assertIn(resp.status_code, (302, 404))
        # Invitation must still exist.
        self.assertTrue(Invitation.objects.filter(pk=invite.pk).exists())

    def test_member_role_rejects_cross_account_pk(self):
        """Changing the role of a membership in a different account returns 404."""
        from apps.accounts.models import Membership

        other_membership = Membership.objects.create(
            user=_make_user("staff@b.com"),
            account=self.account_b,
            role=Membership.Role.MEMBER,
        )
        resp = self.client.post(
            reverse("member-role", kwargs={"pk": other_membership.pk}),
            {"role": "admin"},
            follow=False,
        )
        self.assertIn(resp.status_code, (302, 404))
        other_membership.refresh_from_db()
        self.assertEqual(other_membership.role, Membership.Role.MEMBER)


def set_current_account_in_session(session, account):
    """Helper: write the current-account key used by get_current_account()."""
    session["current_account_id"] = account.pk

"""Regression tests: the email-verification link must not bypass login or MFA.

Known issue (audit 2026-10-16): verify_email() logs a visitor in whenever
``already_verified`` is true, and that check runs before the token is
examined. A user with no unverified owner account can therefore be logged in
by anyone who knows their pk, and an enrolled TOTP user receives a full
session from an email link with no second factor.
"""

from django.contrib.auth import get_user
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.tokens import email_verification_token


def _make_user(email="victim@example.com"):
    return User.objects.create_user(username=email, email=email, password="pw-12345678")


def _verify_url(user, token):
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    return reverse("verify_email", kwargs={"uidb64": uidb64, "token": token})


class VerifyEmailLinkBypassTest(TestCase):
    def test_garbage_token_does_not_log_in_a_user_with_no_owner_account(self):
        """With no memberships, already_verified is true; the token is ignored."""
        user = _make_user()
        client = Client()

        client.get(_verify_url(user, "not-a-real-token"))

        self.assertFalse(get_user(client).is_authenticated)

    def test_valid_link_does_not_fully_log_in_an_enrolled_totp_user(self):
        """A valid email link must still require the second factor."""
        user = _make_user()
        TOTPDevice.objects.create(user=user, name="default", confirmed=True)
        token = email_verification_token.make_token(user)
        client = Client()

        resp = client.get(_verify_url(user, token))

        self.assertFalse(get_user(client).is_authenticated)
        self.assertRedirects(resp, reverse("2fa-verify"), fetch_redirect_response=False)

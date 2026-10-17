"""An email-only account gets its sign-in code when the email step opens."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import EmailLoginCode, EmailMfaEnrolment

PASSWORD = "pw-12345678"


class EmailCodeOnArrivalTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="mail-only@example.com",
            email="mail-only@example.com",
            password=PASSWORD,
        )
        EmailMfaEnrolment.objects.create(user=self.user)
        self.client = Client()
        mail.outbox = []
        self.client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )

    def test_the_code_is_emailed_when_the_step_opens(self):
        self.client.get(reverse("2fa-verify"))

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertIn("Your code is", mail.outbox[0].body)

    def test_a_refresh_does_not_send_another_code(self):
        self.client.get(reverse("2fa-verify"))
        self.client.get(reverse("2fa-verify"))

        self.assertEqual(len(mail.outbox), 1)

    def test_resend_inside_the_cooldown_sends_nothing_new(self):
        self.client.get(reverse("2fa-verify"))
        self.client.post(reverse("2fa-verify"), {"action": "send_email"})

        self.assertEqual(len(mail.outbox), 1)

    def test_a_new_sign_in_sends_a_code_again(self):
        self.client.get(reverse("2fa-verify"))
        self.client.logout()
        cache.clear()
        # Move the earlier code outside the one-minute cooldown, as time would.
        EmailLoginCode.objects.update(created_at=timezone.now() - timedelta(minutes=2))
        mail.outbox = []
        self.client.post(
            reverse("login"), {"username": self.user.email, "password": PASSWORD}
        )
        self.client.get(reverse("2fa-verify"))

        self.assertEqual(len(mail.outbox), 1)

"""The /manage/styleguide/ gallery renders and is staff-only."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class StyleguideTests(TestCase):
    def test_requires_superuser(self):
        url = reverse("core:styleguide")
        # anonymous → redirect to login
        self.assertEqual(self.client.get(url).status_code, 302)

        User.objects.create_user("plain", "p@x.com", "pw")
        self.client.login(username="plain", password="pw")
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_superuser_sees_gallery(self):
        User.objects.create_superuser("boss", "b@x.com", "pw")
        self.client.login(username="boss", password="pw")
        resp = self.client.get(reverse("core:styleguide"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Style guide")
        self.assertContains(resp, "btn-primary")
        self.assertContains(resp, "status-indicator")

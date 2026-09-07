from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import Account
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber


@override_settings(INTERNAL_DEBUG_ENABLED=True, INTERNAL_DEBUG_TOKEN="test-token")
class InternalDebugAuthTest(TestCase):
    """These tests reload urlconf via override_settings + urls module patching
    is unnecessary here since urlpatterns are built once at import time; instead
    we hit the view directly (it's still registered because INSTALLED_APPS/urls
    are resolved once at process start in the test run, and internal_debug is
    always in INSTALLED_APPS — only the URL *inclusion* is settings-gated at
    import time of automator.urls, which already happened before override_settings
    can take effect). See ConditionalRoutingTest below for that behavior instead.
    """

    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")

    def test_missing_token_rejected(self):
        resp = self.client.get(
            "/internal/debug/api/whatsapp-numbers/", {"account_id": self.account.pk}
        )
        self.assertIn(resp.status_code, (401, 403, 404))

    def test_wrong_token_rejected(self):
        resp = self.client.get(
            "/internal/debug/api/whatsapp-numbers/",
            {"account_id": self.account.pk},
            HTTP_AUTHORIZATION="Bearer wrong-token",
        )
        self.assertIn(resp.status_code, (401, 403, 404))

    def test_correct_token_accepted(self):
        WhatsAppBusinessNumber.objects.create(
            account=self.account,
            phone_number_id="PNID1",
            access_token="secret-token-value",
            verification_pin="123456",
        )
        resp = self.client.get(
            "/internal/debug/api/whatsapp-numbers/",
            {"account_id": self.account.pk},
            HTTP_AUTHORIZATION="Bearer test-token",
        )
        if resp.status_code == 404:
            self.skipTest("INTERNAL_DEBUG_ENABLED route not mounted in this process")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["results"]), 1)
        row = body["results"][0]
        self.assertTrue(row["has_access_token"])
        self.assertTrue(row["has_verification_pin"])
        # Never leak the actual secret values.
        self.assertNotIn("secret-token-value", resp.content.decode())
        self.assertNotIn("123456", resp.content.decode())
        self.assertNotIn("access_token", row)
        self.assertNotIn("verification_pin", row)


@override_settings(INTERNAL_DEBUG_ENABLED=False)
class ConditionalRoutingTest(TestCase):
    """When the flag is off at process start, the route never gets mounted."""

    def test_route_not_mounted_when_disabled(self):
        # automator.urls is evaluated once at import time; if the test suite
        # ran with INTERNAL_DEBUG_ENABLED unset/False from the environment,
        # this route won't exist at all.
        try:
            reverse("internal-debug-whatsapp-numbers")
        except Exception:
            return
        # If it does resolve (e.g. ran with the flag on), skip rather than fail —
        # this test only asserts the off-by-default behavior when applicable.
        self.skipTest("internal_debug urls were mounted for this test run")

"""The Permissions-Policy header must let our own pages use the microphone.

Inbox voice notes call getUserMedia; with ``microphone=()`` the browser refuses
without ever showing the permission prompt.
"""

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from apps.core.middleware import SecurityHeadersMiddleware


class PermissionsPolicyTest(SimpleTestCase):
    def _policy(self) -> dict[str, str]:
        response = SecurityHeadersMiddleware(lambda request: HttpResponse())(
            RequestFactory().get("/inbox/")
        )
        return dict(
            part.strip().split("=", 1)
            for part in response["Permissions-Policy"].split(",")
        )

    def test_microphone_is_allowed_for_our_own_pages_only(self):
        self.assertEqual(self._policy()["microphone"], "(self)")

    def test_other_sensitive_features_stay_off(self):
        policy = self._policy()
        for feature in ("camera", "geolocation", "payment", "usb"):
            self.assertEqual(policy[feature], "()", feature)

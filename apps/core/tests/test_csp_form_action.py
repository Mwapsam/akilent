"""The CSP's form-action must allow every payment provider's hosted checkout.

Browsers apply form-action to each redirect after a form submit, so signup
(POST) -> checkout (302) -> provider (302) is blocked unless the provider's
origin is listed: the browser stays on the signup page with no error shown.
"""

from django.test import SimpleTestCase

from apps.billing.providers import registry
from apps.core.middleware import _build_csp


class FormActionTest(SimpleTestCase):
    def _form_action(self) -> list[str]:
        for directive in _build_csp().split(";"):
            name, _, sources = directive.strip().partition(" ")
            if name == "form-action":
                return sources.split()
        self.fail("no form-action directive")

    def test_our_own_forms_are_allowed(self):
        self.assertIn("'self'", self._form_action())

    def test_every_provider_checkout_origin_is_allowed(self):
        allowed = self._form_action()
        for provider in registry.all_providers():
            for origin in provider.checkout_origins:
                self.assertIn(origin, allowed, provider.code)

    def test_every_provider_that_redirects_off_site_declares_its_origins(self):
        # Manual bank transfer stays on our own pages; every selectable provider hands the
        # customer to its hosted page.
        for provider in registry.selectable():
            self.assertTrue(provider.checkout_origins, provider.code)

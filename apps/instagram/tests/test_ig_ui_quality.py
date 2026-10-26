"""UI quality programme, Phase 4: the Instagram pages say what state things are in.

A working connection was badged "Sent" (it borrowed the message-status component), and
every flash message was shown twice: once inline, once as the shell's toast.
"""

from django.test import TestCase

from apps.billing.models import AccountFeatureOverride
from apps.instagram.tests.helpers import make_account, make_instagram_account


class InstagramAccountsPageTest(TestCase):
    def setUp(self):
        self.account, self.user = make_account()
        self.iba = make_instagram_account(self.account)
        AccountFeatureOverride.objects.create(
            account=self.account, key="instagram", grant=True, note="test"
        )
        self.client.force_login(self.user)

    def test_a_working_connection_says_connected_not_sent(self):
        body = self.client.get("/instagram/accounts/").content.decode()
        self.assertIn('badge-success">Connected', body)
        self.assertNotIn(">Sent<", body.replace("\n", "").replace(" ", ""))

    def test_an_expired_connection_says_so(self):
        self.iba.token_expired = True
        self.iba.save(update_fields=["token_expired"])
        body = self.client.get("/instagram/accounts/").content.decode()
        self.assertIn('badge-danger">Connection expired', body)
        self.assertIn("Reconnect to fix it", body)

    def test_a_flash_message_is_shown_once(self):
        """base.html turns it into a toast; the page no longer prints its own copy."""
        from django.contrib import messages
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib.sessions.backends.db import SessionStore
        from django.test import RequestFactory

        from apps.instagram.views import instagram_accounts

        request = RequestFactory().get("/instagram/accounts/")
        request.user = self.user
        request.session = SessionStore()
        request._messages = FallbackStorage(request)
        messages.success(request, "Instagram connected: qa_once")
        body = instagram_accounts(request).content.decode()
        self.assertEqual(body.count("Instagram connected: qa_once"), 1)

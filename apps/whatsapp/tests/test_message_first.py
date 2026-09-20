"""Guided "message us first" step: unlocks a plain-text test on any number."""
import html
import json
from datetime import timedelta
from unittest.mock import patch

import responses
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Account
from apps.whatsapp import numbers as numbers_views
from apps.whatsapp.embedded import fetch_display_number
from apps.whatsapp.models import Conversation, MessageLog
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber as N
from apps.whatsapp.setup import build_setup_console

R = N.RegistrationStatus
TESTER = "+260971234567"
FETCH = "apps.whatsapp.embedded.fetch_display_number"


class Base(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        self.number = N.objects.create(
            account=self.account, phone_number_id="PNID", access_token="tok", waba_id="W",
            registration_status=R.REGISTERED, display_number="+1 555-025-3483",
        )

    def inbound(self, phone=TESTER, ago=timedelta(minutes=1)):
        contact = self.account.contacts.create(phone_number=phone)
        return MessageLog.objects.create(
            account=self.account, conversation=Conversation.get_or_open(contact), contact=contact,
            direction=MessageLog.Direction.INBOUND, message_type="text", content="Hi",
            status="delivered", timestamp=timezone.now() - ago,
        )

    def console(self):
        return build_setup_console([self.number], embedded_enabled=True, inbound_seen=False)

    def step(self, key):
        return next(s for s in self.console().steps if s.key == key)


class MessageFirstStepTest(Base):
    def test_registered_number_is_first_asked_to_message_us(self):
        c = self.console()
        self.assertEqual(c.current.key, "message_first")
        self.assertEqual(c.primary_action["kind"], "guide")
        self.assertEqual(c.primary_action["url"], "https://wa.me/15550253483?text=Hi")
        self.assertIn("+1 555-025-3483", c.current.hint)
        self.assertEqual(self.step("test").state, "upcoming")

    def test_no_link_when_display_number_unknown(self):
        self.number.display_number = None
        c = self.console()
        self.assertEqual(c.current.key, "message_first")
        self.assertEqual(c.primary_action["url"], "")

    def test_recent_message_completes_step_and_prefills_the_test(self):
        self.inbound()
        c = self.console()
        self.assertEqual(self.step("message_first").state, "done")
        self.assertEqual(c.current.key, "test")
        self.assertEqual(c.primary_action["prefill"], TESTER)
        self.assertIn(TESTER, self.step("message_first").hint)

    def test_old_message_does_not_count(self):
        self.inbound(ago=timedelta(hours=30))
        self.assertEqual(self.console().current.key, "message_first")

    def test_already_tested_numbers_are_not_sent_backwards(self):
        self.number.connection_tests.create(recipient=TESTER, status="sent")
        c = self.console()
        self.assertEqual(self.step("message_first").state, "done")
        self.assertIsNone(c.current)

    def test_unregistered_number_has_no_guide_action(self):
        self.number.registration_status = R.FAILED
        self.assertIsNone(self.step("message_first").action)


class StatusEndpointTest(Base):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("u", password="p")
        self.other = Account.objects.create(company_name="Other", slug="other")

    def _get(self, pk):
        request = RequestFactory().get(f"/whatsapp/numbers/{pk}/status/")
        request.user = self.user
        with patch("apps.whatsapp.numbers.get_current_account", return_value=self.account):
            return numbers_views.numbers_status(request, pk)

    def test_reports_waiting(self):
        body = json.loads(self._get(self.number.pk).content)
        self.assertEqual((body["message_received"], body["stage"], body["sender"]),
                         (False, "waiting", ""))

    def test_reports_received_with_sender(self):
        self.inbound()
        body = json.loads(self._get(self.number.pk).content)
        self.assertEqual((body["message_received"], body["stage"], body["sender"]),
                         (True, "received", TESTER))

    def test_other_accounts_number_404(self):
        foreign = N.objects.create(account=self.other, phone_number_id="X")
        with self.assertRaises(Http404):
            self._get(foreign.pk)


@override_settings(WHATSAPP_GRAPH_VERSION="v21.0")
class FetchDisplayNumberTest(TestCase):
    URL = "https://graph.facebook.com/v21.0/PNID"

    @responses.activate
    def test_returns_display_number(self):
        responses.add(responses.GET, self.URL, json={"display_phone_number": "+1 555-025-3483"})
        self.assertEqual(fetch_display_number("PNID", "tok"), "+1 555-025-3483")

    @responses.activate
    def test_error_returns_empty(self):
        responses.add(responses.GET, self.URL, json={"error": {}}, status=400)
        self.assertEqual(fetch_display_number("PNID", "tok"), "")

    @responses.activate
    def test_network_error_returns_empty(self):
        import requests

        responses.add(responses.GET, self.URL, body=requests.ConnectionError("down"))
        self.assertEqual(fetch_display_number("PNID", "tok"), "")


class EnsureDisplayNumberTest(Base):
    def test_fetches_and_saves_once_when_missing(self):
        self.number.display_number = None
        self.number.save()
        with patch(FETCH, return_value="+1 555-000-1111") as f:
            numbers_views._ensure_display_number([self.number])
            numbers_views._ensure_display_number([self.number])
        f.assert_called_once()
        self.number.refresh_from_db()
        self.assertEqual(self.number.display_number, "+1 555-000-1111")

    def test_skips_when_known_or_already_tested(self):
        with patch(FETCH) as f:
            numbers_views._ensure_display_number([self.number])  # known
            self.number.display_number = None
            self.number.connection_tests.create(recipient=TESTER, status="sent")
            numbers_views._ensure_display_number([self.number])  # tested
        f.assert_not_called()


class PageRenderTest(Base):
    def _render(self):
        request = RequestFactory().get("/whatsapp/numbers/")
        request.user = User.objects.create_user("u", password="p")
        request.session = {}
        request._messages = FallbackStorage(request)
        with patch("apps.whatsapp.numbers.get_current_account", return_value=self.account), patch(
            "apps.billing.api.has_feature", return_value=True
        ), patch(FETCH, return_value=""):
            return html.unescape(numbers_views.numbers_list(request).content.decode())

    def test_guide_is_rendered_with_link_polling_and_skip(self):
        body = self._render()
        self.assertIn('href="https://wa.me/15550253483?text=Hi"', body)
        self.assertIn("Open WhatsApp", body)
        self.assertIn(f'data-status-url="/whatsapp/numbers/{self.number.pk}/status/"', body)
        self.assertIn("Skip — send a template test instead", body)

    def test_polling_keeps_running_in_a_background_tab(self):
        body = self._render()
        self.assertNotIn("if (document.hidden) return", body)  # would pause while they use WhatsApp
        self.assertIn("visibilitychange", body)  # and re-check as soon as they return

    def test_test_form_is_prefilled_after_they_message_us(self):
        self.inbound()
        body = self._render()
        self.assertIn(f'value="{TESTER}"', body)
        self.assertNotIn("data-status-url=", body)  # the poll element is gone once they messaged

"""Tech Provider onboarding — Embedded Signup completion.

Per Meta's Tech Provider Program a number handed over by Embedded Signup is not
usable on the Cloud API until the Tech Provider (1) subscribes its app to the
WABA and (2) registers the phone number. `connect_complete` must do both and
persist the number with its access token + verification PIN.
"""
import json
from unittest.mock import patch

import responses
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase, override_settings

from apps.accounts.models import Account
from apps.whatsapp import numbers as numbers_views
from apps.whatsapp.embedded import EmbeddedSignupError, register_phone_number
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber


@override_settings(WHATSAPP_GRAPH_VERSION="v21.0")
class RegisterPhoneNumberTest(TestCase):
    @responses.activate
    def test_register_success(self):
        responses.add(
            responses.POST,
            "https://graph.facebook.com/v21.0/PNID/register",
            json={"success": True}, status=200,
        )
        register_phone_number("PNID", "tok", "123456")
        body = json.loads(responses.calls[0].request.body)
        self.assertEqual(body, {"messaging_product": "whatsapp", "pin": "123456"})

    @responses.activate
    def test_register_failure_raises(self):
        responses.add(
            responses.POST,
            "https://graph.facebook.com/v21.0/PNID/register",
            json={"error": {"message": "PIN required"}}, status=400,
        )
        with self.assertRaises(EmbeddedSignupError):
            register_phone_number("PNID", "tok", "123456")


@override_settings(
    WHATSAPP_APP_ID="app", WHATSAPP_APP_SECRET="secret",
)
class ConnectCompleteTest(TestCase):
    def setUp(self):
        from django.utils import timezone

        from apps.billing.models import Plan, Subscription

        self.account = Account.objects.create(company_name="Co", slug="co")
        plan = Plan.objects.first() or Plan.objects.create(
            name="Test Plan", slug="test-plan", price_monthly=0,
            max_whatsapp_numbers=5,
        )
        Subscription.objects.create(
            account=self.account, plan=plan, status=Subscription.ACTIVE,
            current_period_start=timezone.now(),
        )
        self.user = User.objects.create_user("owner", "o@example.com", "pw")
        self.rf = RequestFactory()

    def _request(self, payload):
        request = self.rf.post(
            "/whatsapp/connect/complete/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        request.user = self.user
        # connect_complete may flash a message (e.g. registration-failure
        # warning); RequestFactory requests have no session/message
        # middleware, so attach a minimal storage the same way Django's own
        # test docs recommend for view-level message assertions.
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def _post(self, payload):
        request = self._request(payload)
        with patch(
            "apps.whatsapp.numbers.get_current_account", return_value=self.account
        ), patch(
            "apps.whatsapp.embedded.exchange_code_for_token", return_value="TOKEN"
        ) as ex, patch(
            "apps.whatsapp.embedded.subscribe_app_to_waba"
        ) as sub, patch(
            "apps.whatsapp.registration.register_phone_number"
        ) as reg:
            response = numbers_views.connect_complete(request)
        return response, ex, sub, reg

    def test_completes_onboarding_and_registers_number(self):
        response, ex, sub, reg = self._post(
            {"code": "abc", "phone_number_id": "PNID", "waba_id": "WABA1"}
        )
        self.assertEqual(response.status_code, 200)
        ex.assert_called_once_with("abc")
        sub.assert_called_once_with("WABA1", "TOKEN")
        reg.assert_called_once()
        self.assertEqual(reg.call_args[0][0], "PNID")
        self.assertEqual(reg.call_args[0][1], "TOKEN")
        self.assertRegex(reg.call_args[0][2], r"^\d{6}$")  # generated PIN

        number = WhatsAppBusinessNumber.objects.get(phone_number_id="PNID")
        self.assertEqual(number.account_id, self.account.pk)
        self.assertEqual(number.access_token, "TOKEN")
        self.assertEqual(number.waba_id, "WABA1")
        self.assertRegex(number.verification_pin, r"^\d{6}$")
        self.assertEqual(number.registration_status, "registered")
        self.assertEqual(response.content.decode().count("/whatsapp/numbers/"), 1)

    def test_registration_failure_still_connects_without_pin(self):
        with patch(
            "apps.whatsapp.numbers.get_current_account", return_value=self.account
        ), patch(
            "apps.whatsapp.embedded.exchange_code_for_token", return_value="TOKEN"
        ), patch(
            "apps.whatsapp.embedded.subscribe_app_to_waba"
        ), patch(
            "apps.whatsapp.registration.register_phone_number",
            side_effect=EmbeddedSignupError("nope"),
        ):
            response = numbers_views.connect_complete(
                self._request({"code": "abc", "phone_number_id": "PNID2"})
            )

        self.assertEqual(response.status_code, 200)
        number = WhatsAppBusinessNumber.objects.get(phone_number_id="PNID2")
        self.assertEqual(number.access_token, "TOKEN")
        self.assertIsNone(number.verification_pin)
        self.assertEqual(number.registration_status, "failed")
        self.assertEqual(number.registration_error, "nope")
        self.assertFalse(number.is_ready)

    def test_number_owned_by_another_account_is_rejected(self):
        other = Account.objects.create(company_name="Other", slug="other")
        WhatsAppBusinessNumber.objects.create(
            account=other, phone_number_id="TAKEN", is_active=True
        )
        with patch(
            "apps.whatsapp.numbers.get_current_account", return_value=self.account
        ):
            response = numbers_views.connect_complete(
                self._request({"code": "abc", "phone_number_id": "TAKEN"})
            )
        self.assertEqual(response.status_code, 409)

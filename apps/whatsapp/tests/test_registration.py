"""Registration lifecycle: one idempotent path for OAuth, manual and retry."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings

from apps.accounts.models import Account
from apps.whatsapp.embedded import EmbeddedSignupError
from apps.whatsapp.numbers import numbers_register
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber as N
from apps.whatsapp.registration import register_number

R = N.RegistrationStatus
S = N.SetupStatus
PATCH = "apps.whatsapp.registration.register_phone_number"


def make(account, **kw):
    kw.setdefault("phone_number_id", "PNID")
    kw.setdefault("access_token", "tok")
    kw.setdefault("waba_id", "WABA")
    return N.objects.create(account=account, **kw)


class ModelStateTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")

    def test_pin_is_not_readiness(self):
        n = make(self.account, verification_pin="123456")
        self.assertFalse(n.is_ready)  # PENDING until registered

    def test_registered_with_creds_is_ready(self):
        n = make(self.account, registration_status=R.REGISTERED)
        self.assertTrue(n.is_ready)
        self.assertEqual(n.setup_status, S.READY_FOR_TEST)

    def test_revoked_token_is_not_ready(self):
        n = make(self.account, registration_status=R.REGISTERED, access_token=None)
        self.assertFalse(n.is_ready)
        self.assertEqual(n.setup_status, S.ACTION_REQUIRED)

    def test_failed_status(self):
        n = make(self.account, registration_status=R.FAILED)
        self.assertEqual(n.setup_status, S.FAILED)


class RegisterNumberTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")

    def test_success_persists_status_and_pin(self):
        n = make(self.account)
        with patch(PATCH) as reg:
            r = register_number(n)
        self.assertTrue(r.ok)
        n.refresh_from_db()
        self.assertEqual(n.registration_status, R.REGISTERED)
        self.assertEqual(len(n.verification_pin), 6)
        reg.assert_called_once_with("PNID", "tok", n.verification_pin)

    def test_idempotent_when_registered(self):
        n = make(self.account, registration_status=R.REGISTERED)
        with patch(PATCH) as reg:
            r = register_number(n)
        self.assertTrue(r.ok and r.already_registered)
        reg.assert_not_called()

    def test_failure_then_retry_reuses_pin(self):
        n = make(self.account, verification_pin="654321")
        with patch(PATCH, side_effect=EmbeddedSignupError("133010")):
            r = register_number(n)
        self.assertFalse(r.ok)
        n.refresh_from_db()
        self.assertEqual(n.registration_status, R.FAILED)
        self.assertEqual(n.registration_error, "133010")
        with patch(PATCH) as reg:
            self.assertTrue(register_number(n).ok)
        reg.assert_called_once_with("PNID", "tok", "654321")
        n.refresh_from_db()
        self.assertEqual(n.registration_status, R.REGISTERED)
        self.assertEqual(n.registration_error, "")

    def test_network_error_is_not_left_registering(self):
        n = make(self.account)
        with patch(PATCH, side_effect=ConnectionError("boom")):
            self.assertFalse(register_number(n).ok)
        n.refresh_from_db()
        self.assertEqual(n.registration_status, R.FAILED)

    def test_no_token_makes_no_call(self):
        n = make(self.account, access_token=None)
        with patch(PATCH) as reg:
            self.assertFalse(register_number(n).ok)
        reg.assert_not_called()


@override_settings(ROOT_URLCONF="apps.whatsapp.tests.urls_enabled")
class RetryEndpointTest(TestCase):

    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        self.other = Account.objects.create(company_name="Other", slug="other")
        self.user = User.objects.create_user("u", password="p")
        self.number = make(self.account, registration_status=R.FAILED)

    def _post(self, pk):
        request = RequestFactory().post(f"/whatsapp/numbers/{pk}/register/")
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        with patch("apps.whatsapp.numbers.get_current_account", return_value=self.account):
            return numbers_register(request, pk)

    def test_retry_registers_and_second_click_is_noop(self):
        with patch(PATCH) as reg:
            self.assertEqual(self._post(self.number.pk).status_code, 302)
            self._post(self.number.pk)
        self.assertEqual(reg.call_count, 1)
        self.number.refresh_from_db()
        self.assertTrue(self.number.is_ready)

    def test_other_accounts_number_404(self):
        foreign = make(self.other, phone_number_id="OTHER")
        with self.assertRaises(Http404):
            self._post(foreign.pk)


class SetupConsoleTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")

    def _build(self, numbers, inbound=False):
        from apps.whatsapp.setup import build_setup_console

        return build_setup_console(numbers, embedded_enabled=True, inbound_seen=inbound)

    def test_no_number_points_at_connect(self):
        c = self._build([])
        self.assertEqual(c.current.key, "connected")
        self.assertEqual(c.primary_action["url"], "/whatsapp/connect/redirect/")

    def test_failed_registration_current_step_is_retry(self):
        c = self._build([make(self.account, registration_status=R.FAILED)])
        self.assertEqual(c.current.key, "registered")
        self.assertEqual(c.primary_action["label"], "Retry registration")
        self.assertEqual(c.primary_action["method"], "post")
        self.assertEqual([s.state for s in c.steps][-1], "optional")

    def test_exactly_one_current_step(self):
        c = self._build([make(self.account, access_token=None)])
        self.assertEqual([s.state for s in c.steps].count("current"), 1)
        self.assertEqual(c.current.key, "credentials")

    def test_registered_number_is_asked_for_a_test_message(self):
        c = self._build([make(self.account, registration_status=R.REGISTERED)])
        self.assertEqual(c.current.key, "test")
        self.assertEqual(c.primary_action["kind"], "verify")
        self.assertFalse(c.required_complete)

    def test_tested_number_completes_required_steps_without_inbound(self):
        n = make(self.account, registration_status=R.REGISTERED)
        n.connection_tests.create(recipient="+260971234567", status="sent")
        c = self._build([n], inbound=False)
        self.assertTrue(c.required_complete)
        self.assertIsNone(c.current)
        self.assertEqual([s.state for s in c.steps][-1], "optional")

    def test_picks_non_ready_number(self):
        ready = make(self.account, phone_number_id="A", registration_status=R.REGISTERED)
        broken = make(self.account, phone_number_id="B", registration_status=R.FAILED)
        self.assertEqual(self._build([ready, broken]).number.pk, broken.pk)

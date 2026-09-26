"""Connect-flow error recovery: every failure branch stores a structured error
and the numbers page renders it with a next action."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from apps.accounts.models import Account
from apps.whatsapp.embedded import EmbeddedSignupError
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.setup_errors import SESSION_KEY, SETUP_ERRORS, SetupError

CALLBACK = "/whatsapp/connect/redirect/callback/"
EMB = "apps.whatsapp.embedded"


@override_settings(
    ROOT_URLCONF="apps.whatsapp.tests.urls_enabled",
    WHATSAPP_APP_ID="app", WHATSAPP_CONFIG_ID="cfg", WHATSAPP_GRAPH_VERSION="v21.0",
)
class ConnectCallbackErrorTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Co", slug="co")
        self.user = User.objects.create_user("u", password="p")
        self.client.force_login(self.user)
        p = patch("apps.whatsapp.numbers.get_current_account", return_value=self.account)
        p.start()
        self.addCleanup(p.stop)

        from django.utils import timezone

        from apps.billing.models import Plan, Subscription

        plan = Plan.objects.first() or Plan.objects.create(
            name="Test Plan", slug="test-plan", price_monthly=0, max_whatsapp_numbers=5,
        )
        Subscription.objects.create(
            account=self.account, plan=plan, status=Subscription.ACTIVE,
            current_period_start=timezone.now(),
        )

    def _numbers_page(self):
        """Render the page view directly: the full layout needs the project urlconf."""
        from django.test import RequestFactory

        from apps.whatsapp import numbers as numbers_views

        request = RequestFactory().get("/whatsapp/numbers/")
        request.user = self.user
        request.session = self.client.session
        with patch("apps.billing.api.entitled", return_value=True):
            return numbers_views.numbers_list(request).content.decode()

    def _arm_state(self):
        s = self.client.session
        s["whatsapp_connect_state"] = "nonce"
        s["whatsapp_connect_redirect_uri"] = "http://testserver" + CALLBACK
        s.save()

    def _stored(self):
        return self.client.session.get(SESSION_KEY, {}).get("code")

    def test_every_error_code_has_presentation(self):
        for code in SetupError:
            self.assertIn(code, SETUP_ERRORS)
            self.assertTrue(SETUP_ERRORS[code]["title"])

    def test_state_mismatch(self):
        self._arm_state()
        r = self.client.get(CALLBACK, {"state": "wrong", "code": "c"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._stored(), SetupError.STATE_EXPIRED)

    def test_cancelled_shows_connect_cta_and_meta_detail(self):
        self._arm_state()
        self.client.get(CALLBACK, {"state": "nonce", "error_description": "User denied"})
        self.assertEqual(self._stored(), SetupError.CANCELLED)
        body = self._numbers_page()
        self.assertIn("WhatsApp setup wasn&#x27;t completed", body)
        self.assertIn("User denied", body)
        self.assertIn('href="/whatsapp/connect/redirect/"', body)

    def test_error_survives_reload_until_new_attempt(self):
        self._arm_state()
        self.client.get(CALLBACK, {"state": "nonce", "error": "access_denied"})
        for _ in range(2):
            self.assertIn("setup wasn", self._numbers_page())
        self.client.get("/whatsapp/connect/redirect/")  # starting again clears it
        self.assertIsNone(self._stored())

    def test_missing_code(self):
        self._arm_state()
        self.client.get(CALLBACK, {"state": "nonce"})
        self.assertEqual(self._stored(), SetupError.NO_CODE)

    def test_token_exchange_failure_keeps_meta_message(self):
        self._arm_state()
        with patch(f"{EMB}.exchange_code_for_token", side_effect=EmbeddedSignupError("bad code")):
            self.client.get(CALLBACK, {"state": "nonce", "code": "c"})
        stored = self.client.session[SESSION_KEY]
        self.assertEqual(stored["code"], SetupError.TOKEN_EXCHANGE_FAILED)
        self.assertEqual(stored["detail"], "bad code")

    def _discover(self, wabas, phones):
        return patch(f"{EMB}.discover_waba_and_phone", return_value=(wabas, phones))

    def test_no_waba(self):
        self._arm_state()
        with patch(f"{EMB}.exchange_code_for_token", return_value="T"), self._discover([], {}):
            self.client.get(CALLBACK, {"state": "nonce", "code": "c"})
        self.assertEqual(self._stored(), SetupError.NO_WABA)

    def test_waba_without_phone(self):
        self._arm_state()
        with patch(f"{EMB}.exchange_code_for_token", return_value="T"), self._discover(["W"], {}):
            self.client.get(CALLBACK, {"state": "nonce", "code": "c"})
        self.assertEqual(self._stored(), SetupError.NO_PHONE)

    def test_number_owned_by_other_account_is_rejected(self):
        other = Account.objects.create(company_name="Other", slug="other")
        WhatsAppBusinessNumber.objects.create(account=other, phone_number_id="P1")
        self._arm_state()
        with patch(f"{EMB}.exchange_code_for_token", return_value="T"), self._discover(
            ["W"], {"W": [{"id": "P1"}]}
        ):
            self.client.get(CALLBACK, {"state": "nonce", "code": "c"})
        stored = self.client.session[SESSION_KEY]
        self.assertEqual(stored["code"], SetupError.CONNECT_REJECTED)
        self.assertIn("another account", stored["detail"])

    def test_successful_connect_clears_previous_error(self):
        s = self.client.session
        s[SESSION_KEY] = {"code": SetupError.CANCELLED, "detail": ""}
        s.save()
        self._arm_state()
        with patch(f"{EMB}.exchange_code_for_token", return_value="T"), self._discover(
            ["W"], {"W": [{"id": "P1"}]}
        ), patch(f"{EMB}.subscribe_app_to_waba"), patch(
            "apps.whatsapp.registration.register_phone_number"
        ):
            r = self.client.get(CALLBACK, {"state": "nonce", "code": "c"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r["Location"], "/whatsapp/numbers/")
        self.assertIsNone(self._stored())
        self.assertTrue(WhatsAppBusinessNumber.objects.get(phone_number_id="P1").is_ready)

    def test_select_expired(self):
        r = self.client.post("/whatsapp/connect/redirect/select/", {"phone_number_id": "x"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._stored(), SetupError.SELECTION_EXPIRED)

    def test_start_when_unconfigured(self):
        with override_settings(WHATSAPP_APP_ID="", WHATSAPP_CONFIG_ID=""):
            self.client.get("/whatsapp/connect/redirect/")
        self.assertEqual(self._stored(), SetupError.NOT_CONFIGURED)

"""A manually added WhatsApp number must be proven to belong to the account.

Inbound messages are routed by phone_number_id alone, so an unverified id would let
one account claim another business's number and receive its customers' messages.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Account, Membership
from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
from apps.whatsapp.registration import RegistrationResult


class ManualNumberOwnershipTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Own Co", slug="own-co")
        self.user = User.objects.create_user("owner@x.com", "owner@x.com", "x")
        Membership.objects.create(
            user=self.user, account=self.account, role=Membership.Role.OWNER
        )
        self.client.force_login(self.user)
        limit = patch("apps.billing.limits.LimitChecker.check_whatsapp_number")
        limit.start()
        self.addCleanup(limit.stop)

    def _create(self, **data):
        return self.client.post(
            reverse("whatsapp-numbers-create"),
            data,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_number_without_a_token_is_rejected(self):
        resp = self._create(phone_number_id="111")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(WhatsAppBusinessNumber.objects.exists())

    def test_number_meta_does_not_confirm_is_rejected(self):
        with patch("apps.whatsapp.embedded.fetch_display_number", return_value=""):
            resp = self._create(phone_number_id="111", access_token="tok")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(WhatsAppBusinessNumber.objects.exists())

    def test_confirmed_number_is_added(self):
        with (
            patch(
                "apps.whatsapp.numbers.register_number",
                return_value=RegistrationResult(ok=True),
            ),
            patch(
                "apps.whatsapp.embedded.fetch_display_number",
                return_value="+260 97 000",
            ),
        ):
            resp = self._create(phone_number_id="111", access_token="tok")
        self.assertEqual(resp.status_code, 200)
        number = WhatsAppBusinessNumber.objects.get(phone_number_id="111")
        self.assertEqual(number.account, self.account)
        self.assertEqual(number.display_number, "+260 97 000")

    def test_member_cannot_add_a_number(self):
        Membership.objects.filter(user=self.user).update(role=Membership.Role.MEMBER)
        with patch(
            "apps.whatsapp.embedded.fetch_display_number", return_value="+260 97 000"
        ):
            resp = self._create(phone_number_id="111", access_token="tok")
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(WhatsAppBusinessNumber.objects.exists())

    def test_member_cannot_remove_a_number(self):
        number = WhatsAppBusinessNumber.objects.create(
            account=self.account, phone_number_id="222", access_token="tok"
        )
        Membership.objects.filter(user=self.user).update(role=Membership.Role.MEMBER)
        self.client.post(reverse("whatsapp-numbers-delete", args=[number.pk]))
        self.assertTrue(WhatsAppBusinessNumber.objects.filter(pk=number.pk).exists())

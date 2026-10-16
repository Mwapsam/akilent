"""Role-based MFA enforcement: off by default, and correct when switched on."""

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import Account, Membership
from apps.accounts.tests.test_2fa import _current_token

PASSWORD = "pw-12345678"
PROTECTED = "settings-security"


def _user(email, staff=False):
    user = User.objects.create_user(username=email, email=email, password=PASSWORD)
    user.is_staff = staff
    user.save()
    return user


class EnforcementOffByDefaultTest(TestCase):
    def test_staff_without_device_reaches_protected_page_when_off(self):
        staff = _user("staff-off@example.com", staff=True)
        client = Client()
        client.force_login(staff)

        resp = client.get(reverse(PROTECTED))

        self.assertEqual(resp.status_code, 200)


@override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCE_STAFF=True)
class StaffEnforcementTest(TestCase):
    def setUp(self):
        self.staff = _user("staff-on@example.com", staff=True)
        self.client = Client()

    def test_staff_without_device_is_sent_to_setup(self):
        self.client.force_login(self.staff)

        resp = self.client.get(reverse(PROTECTED))

        self.assertRedirects(resp, reverse("2fa-setup"), fetch_redirect_response=False)

    def test_setup_page_stays_reachable_without_a_device(self):
        self.client.force_login(self.staff)

        resp = self.client.get(reverse("2fa-setup"))

        self.assertEqual(resp.status_code, 200)

    def test_open_session_with_device_but_no_code_is_sent_to_the_prompt(self):
        TOTPDevice.objects.create(user=self.staff, name="default", confirmed=True)
        self.client.force_login(self.staff)

        resp = self.client.get(reverse(PROTECTED))

        self.assertRedirects(resp, reverse("2fa-verify"), fetch_redirect_response=False)

    def test_enforced_user_reaches_setup_in_one_hop_not_a_loop(self):
        """Following redirects must end on the set-up page, never cycle."""
        self.client.force_login(self.staff)

        resp = self.client.get(reverse(PROTECTED), follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.redirect_chain[-1][0], reverse("2fa-setup"))
        self.assertLessEqual(len(resp.redirect_chain), 1)

    def test_password_plus_code_reaches_protected_page(self):
        device = TOTPDevice.objects.create(
            user=self.staff, name="default", confirmed=True
        )
        self.client.post(
            reverse("login"), {"username": "staff-on@example.com", "password": PASSWORD}
        )
        self.client.post(reverse("2fa-verify"), {"token": _current_token(device)})

        resp = self.client.get(reverse(PROTECTED))

        self.assertEqual(resp.status_code, 200)


@override_settings(MFA_ENFORCEMENT_ENABLED=True, MFA_ENFORCED_ROLES=("owner",))
class RoleEnforcementTest(TestCase):
    def setUp(self):
        self.account = Account.objects.create(company_name="Enforced Co")
        self.client = Client()

    def _member(self, email, role):
        user = _user(email)
        Membership.objects.create(user=user, account=self.account, role=role)
        return user

    def test_owner_without_device_is_sent_to_setup(self):
        owner = self._member("owner@example.com", Membership.Role.OWNER)
        self.client.force_login(owner)

        resp = self.client.get(reverse(PROTECTED))

        self.assertRedirects(resp, reverse("2fa-setup"), fetch_redirect_response=False)

    def test_member_outside_enforced_roles_is_not_affected(self):
        member = self._member("member@example.com", Membership.Role.MEMBER)
        self.client.force_login(member)

        resp = self.client.get(reverse(PROTECTED))

        self.assertEqual(resp.status_code, 200)

    def test_enforced_role_can_still_leave_via_logout(self):
        owner = self._member("owner-logout@example.com", Membership.Role.OWNER)
        self.client.force_login(owner)

        resp = self.client.post(reverse("logout"))

        self.assertNotEqual(resp.status_code, 500)
        self.assertFalse(self.client.session.get("_auth_user_id"))

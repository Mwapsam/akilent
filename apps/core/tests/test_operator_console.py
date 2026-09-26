"""The Operator Console: separate from the client app, operator-only, audited, and able to act."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.billing.models import ManualPaymentRequest, Plan, Subscription
from apps.contacts.models import Contact
from apps.conversations.models import Conversation, Message
from apps.core.models import AdminAction
from apps.email.models import EmailApiKey, EmailDomain


@pytest.fixture
def operator(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


@pytest.fixture
def business(db):
    owner = User.objects.create_user("owner", "owner@acme.test", "pw")
    account = Account.objects.create(company_name="Acme", slug="acme")
    Membership.objects.create(user=owner, account=account, role=Membership.Role.OWNER)
    return account


@pytest.fixture
def owner_client(client, business):
    client.force_login(business.owner)
    return client


@pytest.fixture
def op_client(client, operator):
    client.force_login(operator)
    return client


PAGES = ["/manage/", "/manage/businesses/", "/manage/payments/", "/manage/plans/", "/manage/audit/",
         "/manage/settings/", "/manage/health/", "/manage/pilot/"]


@pytest.mark.django_db
@pytest.mark.parametrize("url", PAGES)
def test_console_pages_are_for_operators_only(client, operator, business, url):
    assert client.get(url).status_code == 302  # anonymous: log in
    client.force_login(business.owner)
    assert client.get(url).status_code == 403  # a business user: not allowed, no login loop
    client.force_login(operator)
    assert client.get(url).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("tab", ["overview", "billing", "whatsapp", "email", "automations", "ai", "team",
                                 "api", "data", "activity"])
def test_every_business_tab_renders(op_client, business, tab):
    resp = op_client.get(f"/manage/businesses/{business.pk}/tab/{tab}/")
    assert resp.status_code == 200 and "Acme" in resp.content.decode()


@pytest.mark.django_db
def test_an_operator_without_a_workspace_lands_on_the_console(op_client):
    resp = op_client.get("/dashboard/")
    assert resp.status_code == 302 and resp["Location"] == "/manage/"


@pytest.mark.django_db
def test_the_client_nav_has_one_console_link_and_no_admin_group(op_client, operator, business):
    Membership.objects.create(user=operator, account=business, role=Membership.Role.ADMIN)
    html = op_client.get("/dashboard/").content.decode()
    assert "/manage/" in html and "Payment Requests" not in html and "Admin settings" not in html


# ---- suspension is real ----

@pytest.mark.django_db
def test_suspending_locks_the_business_out_everywhere_and_is_audited(op_client, business, client):
    op_client.post(f"/manage/businesses/{business.pk}/do/suspend/")
    business.refresh_from_db()
    assert business.is_active is False
    assert AdminAction.objects.filter(account=business, action="account.suspend").exists()

    from django.test import Client

    member = Client()
    member.force_login(business.owner)
    resp = member.get("/inbox/")
    assert resp.status_code == 403 and "suspended" in resp.content.decode()

    _, raw = EmailApiKey.create_for_account(business, name="default")
    api = Client().get("/api/v1/whatsapp/verification-codes/1", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert api.status_code == 401

    op_client.post(f"/manage/businesses/{business.pk}/do/suspend/")
    business.refresh_from_db()
    assert business.is_active is True
    assert member.get("/settings/").status_code != 403


@pytest.mark.django_db
def test_a_suspended_business_sends_nothing(business):
    from unittest.mock import patch

    from apps.whatsapp.models import OutboundMessage, WhatsAppContact
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber
    from apps.whatsapp.tasks import drain_outbound_queue

    WhatsAppBusinessNumber.objects.create(account=business, phone_number_id="PN", waba_id="W",
                                          access_token="t", is_active=True)
    contact = WhatsAppContact.objects.create(account=business, phone_number="+260971234567")
    msg = OutboundMessage.objects.create(account=business, contact=contact, payload={"type": "text", "body": "hi"})
    Account.objects.filter(pk=business.pk).update(is_active=False)
    provider = type("P", (), {"send_text": lambda *a: (_ for _ in ()).throw(AssertionError("sent"))})()
    with patch("apps.whatsapp.tasks._get_provider_for_account", return_value=provider):
        drain_outbound_queue()
    msg.refresh_from_db()
    assert msg.status == OutboundMessage.Status.FAILED and msg.last_error.startswith("ACCOUNT_SUSPENDED")


# ---- client pages are client-only ----

@pytest.mark.django_db
def test_an_operator_with_a_workspace_sees_only_their_own_email_domains(op_client, operator, business):
    mine = Account.objects.create(company_name="Operator's own", slug="own")
    Membership.objects.create(user=operator, account=mine, role=Membership.Role.OWNER)
    EmailDomain.objects.create(account=business, domain="mail.acme.test")
    EmailDomain.objects.create(account=mine, domain="mail.own.test")
    html = op_client.get("/email/domains/").content.decode()
    assert "mail.own.test" in html and "mail.acme.test" not in html
    assert "every tenant" not in html


@pytest.mark.django_db
def test_the_client_billing_page_has_no_operator_controls(owner_client):
    Plan.objects.create(name="Hidden", slug="hidden", price_monthly=Decimal("5"), is_active=False)
    html = owner_client.get("/billing/plans/").content.decode()
    assert "Add package" not in html and "Hidden" not in html and "Pending bank transfers" not in html


@pytest.mark.django_db
def test_payment_approval_lives_in_the_console_and_is_audited(op_client, business):
    plan = Plan.objects.create(name="Growth", slug="growth", price_monthly=Decimal("10"))
    req = ManualPaymentRequest.objects.create(account=business, plan=plan, reference="MOMO-1")
    resp = op_client.post(f"/manage/payments/{req.pk}/approve/")
    assert resp.status_code == 302 and resp["Location"] == "/manage/payments/"
    req.refresh_from_db()
    assert req.status == ManualPaymentRequest.APPROVED
    assert Subscription.objects.get(account=business).plan == plan
    assert AdminAction.objects.filter(action="payment.approve", account=business).exists()
    assert op_client.post(f"/billing/manual/{req.pk}/approve/").status_code == 404, "old client-side route is gone"


@pytest.mark.django_db
def test_a_business_owner_cannot_approve_payments(owner_client, business):
    plan = Plan.objects.create(name="Growth", slug="growth", price_monthly=Decimal("10"))
    req = ManualPaymentRequest.objects.create(account=business, plan=plan, reference="X")
    assert owner_client.post(f"/manage/payments/{req.pk}/approve/").status_code == 403
    req.refresh_from_db()
    assert req.status == ManualPaymentRequest.PENDING


@pytest.mark.django_db
def test_plan_and_status_can_be_set_for_a_business_with_no_subscription(op_client, business):
    Subscription.objects.filter(account=business).delete()
    plan = Plan.objects.create(name="Growth", slug="growth", price_monthly=Decimal("10"))
    op_client.post(f"/manage/businesses/{business.pk}/do/subscription/", {"plan": plan.pk, "status": "active"})
    assert Subscription.objects.get(account=business).status == "active"


# ---- read-only "View as" ----

def _conversation(account):
    contact = Contact.objects.create(account=account, phone="+260971234567")
    conv = Conversation.objects.create(account=account, contact=contact, channel="whatsapp")
    Message.objects.create(account=account, conversation=conv, direction="inbound", body="Price?",
                           timestamp=timezone.now())
    return conv


@pytest.mark.django_db
def test_view_as_shows_the_business_read_only(op_client, business):
    conv = _conversation(business)
    op_client.post(f"/manage/businesses/{business.pk}/view-as/")
    html = op_client.get("/dashboard/").content.decode()
    assert "Viewing Acme as support" in html

    op_client.get(f"/inbox/{conv.public_id}/")
    conv.refresh_from_db()
    assert conv.is_unread is True, "looking must not clear the business's unread"

    blocked = op_client.post(f"/inbox/{conv.public_id}/", {"body": "hello"})
    assert blocked.status_code == 403
    assert not Message.objects.filter(conversation=conv, direction="outbound").exists()

    op_client.post("/manage/view-as/stop/")
    assert "Viewing Acme" not in op_client.get("/manage/").content.decode()
    actions = list(AdminAction.objects.filter(account=business).values_list("action", flat=True))
    assert "view_as.start" in actions and "view_as.stop" in actions


@pytest.mark.django_db
def test_view_as_expires(op_client, business):
    op_client.post(f"/manage/businesses/{business.pk}/view-as/")
    session = op_client.session
    session["viewing_as"]["started"] = (timezone.now() - timedelta(minutes=61)).isoformat()
    session.save()
    assert "Viewing Acme" not in op_client.get("/manage/").content.decode()


@pytest.mark.django_db
def test_a_business_user_cannot_view_as(owner_client, business):
    other = Account.objects.create(company_name="Other", slug="other")
    assert owner_client.post(f"/manage/businesses/{other.pk}/view-as/").status_code == 403
    session = owner_client.session
    session["viewing_as"] = {"account": other.pk, "started": timezone.now().isoformat()}
    session.save()
    assert "Other" not in owner_client.get("/dashboard/").content.decode()


# ---- data requests ----

@pytest.mark.django_db
def test_contact_export_is_this_business_only_and_audited(op_client, business):
    Contact.objects.create(account=business, first_name="Mwila", phone="+260971111111")
    other = Account.objects.create(company_name="Other", slug="other")
    Contact.objects.create(account=other, first_name="Stranger", phone="+260972222222")
    resp = op_client.post(f"/manage/businesses/{business.pk}/export-contacts/")
    body = resp.content.decode()
    assert resp["Content-Type"].startswith("text/csv") and "Mwila" in body and "Stranger" not in body
    assert AdminAction.objects.filter(action="data.export_contacts", account=business).exists()


@pytest.mark.django_db
def test_deleting_one_customer_needs_confirmation_and_removes_only_them(op_client, business):
    conv = _conversation(business)
    keep = Contact.objects.create(account=business, phone="+260973333333")
    url = f"/manage/businesses/{business.pk}/do/delete_customer/"
    op_client.post(url, {"who": "+260971234567", "confirm": "wrong"})
    assert Conversation.objects.filter(pk=conv.pk).exists()
    op_client.post(url, {"who": "+260971234567", "confirm": "acme"})
    assert not Conversation.objects.filter(pk=conv.pk).exists()
    assert Contact.objects.filter(pk=keep.pk).exists()


@pytest.mark.django_db
def test_closing_suspends_now_and_deletes_after_30_days(op_client, business):
    from apps.core.console.data import delete_due_accounts

    op_client.post(f"/manage/businesses/{business.pk}/do/close/", {"confirm": "acme"})
    business.refresh_from_db()
    assert business.is_active is False and business.scheduled_deletion_at is not None
    assert delete_due_accounts() == 0
    assert delete_due_accounts(now=timezone.now() + timedelta(days=31)) == 1
    assert not Account.objects.filter(pk=business.pk).exists()

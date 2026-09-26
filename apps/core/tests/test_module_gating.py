"""A feature a business can't use cannot execute - on views, workflow enrollment and actions.

"Can't use" is either an operator removal (commercial) or the owner switching an optional tool
off. With neither, a business keeps today's behaviour (allowed), so deploying locks nobody out.
"""
import json
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account, Membership
from apps.automation.models import Workflow, WorkflowRun
from apps.automation.workflow_engine import enroll, enroll_for_trigger
from apps.billing import api as billing_api
from apps.billing.models import Plan, Subscription
from apps.contacts.models import Contact
from apps.core.actions import ActionError, run_action
from apps.email.models import EmailApiKey


@pytest.fixture
def logged_in(client, db):
    user = User.objects.create_user("u", "u@example.com", "pw")
    account = Account.objects.create(company_name="Acme")
    Membership.objects.create(user=user, account=account, role=Membership.Role.OWNER)
    client.force_login(user)
    return client, account


def set_module(account, key, enabled):
    """Optional tools (sales, orders) use the owner's switch; the rest an operator removal."""
    if key in ("sales", "orders"):
        billing_api.set_owner_switch(account, key, enabled)
    elif enabled:
        billing_api.clear_override(account, key)
    else:
        billing_api.set_override(account, key, grant=False, note="test")


@pytest.fixture
def contact(logged_in):
    _, account = logged_in
    return Contact.objects.create(account=account, phone="+260971234567", email="j@x.com")


def published_workflow(account, trigger="contact.created"):
    return Workflow.objects.create(
        account=account, name="WF", status=Workflow.Status.PUBLISHED,
        definition={"trigger": {"type": trigger}, "steps": [
            {"id": "w", "type": "wait", "seconds": 3600, "next": "done"},
            {"id": "done", "type": "stop"}]},
    )


# ---- the gate itself ------------------------------------------------------------
@pytest.mark.django_db
def test_usable_semantics(logged_in):
    _, account = logged_in
    assert billing_api.usable(account, "sales") is True               # nothing stored -> allowed
    set_module(account, "sales", False)
    assert billing_api.usable(account, "sales") is False              # owner switched it off
    assert billing_api.entitled(account, "sales") is True             # ...but still entitled
    set_module(account, "sales", True)
    assert billing_api.usable(account, "sales") is True
    with pytest.raises(KeyError):
        billing_api.usable(account, "not-a-feature")


@pytest.mark.django_db
def test_disabling_one_tenant_does_not_affect_another(logged_in):
    _, account = logged_in
    other = Account.objects.create(company_name="Other")
    set_module(account, "sales", False)
    assert billing_api.usable(other, "sales") is True


# ---- views ----------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("url,module", [("/sales/", "sales"), ("/orders/", "orders"),
                                        ("/automations/", "automations")])
def test_view_allowed_by_default_blocked_when_disabled(logged_in, url, module):
    client, account = logged_in
    assert client.get(url).status_code == 200                        # no row -> unchanged
    set_module(account, module, False)
    resp = client.get(url)
    assert resp.status_code == 302 and resp.url == f"/billing/locked/{module}/"  # the locked page explains
    set_module(account, module, True)
    assert client.get(url).status_code == 200


@pytest.mark.django_db
def test_disabled_module_blocks_detail_and_post_views_too(logged_in, contact):
    from apps.crm.services import create_lead

    client, account = logged_in
    lead = create_lead(account, contact)
    set_module(account, "sales", False)
    assert client.get(f"/sales/leads/{lead.public_id}/").status_code in (302, 403, 404)
    resp = client.post(f"/sales/leads/{lead.public_id}/", {"action": "convert"})
    assert resp.status_code in (302, 403, 404)
    lead.refresh_from_db()
    assert lead.status != "converted"


# ---- workflow enrollment --------------------------------------------------------
@pytest.mark.django_db
def test_disabled_automation_module_blocks_enrollment(logged_in, contact):
    _, account = logged_in
    wf = published_workflow(account)
    set_module(account, "automations", False)
    assert enroll(wf, contact) is None
    assert enroll_for_trigger(account.id, "contact.created", contact) == 0
    assert not WorkflowRun.objects.filter(workflow=wf).exists()


@pytest.mark.django_db
def test_enrollment_still_works_with_no_row_or_enabled(logged_in, contact):  # regression
    _, account = logged_in
    wf = published_workflow(account)
    assert enroll(wf, contact) is not None
    WorkflowRun.objects.all().delete()
    set_module(account, "automations", True)
    assert enroll(wf, contact) is not None


# ---- actions --------------------------------------------------------------------
@pytest.mark.django_db
def test_disabled_crm_module_blocks_crm_actions(logged_in, contact):
    _, account = logged_in
    set_module(account, "sales", False)
    with pytest.raises(ActionError, match="sales"):
        run_action("create_lead", {"account": account}, account=account, contact=contact)


@pytest.mark.django_db
def test_disabled_commerce_module_blocks_commerce_actions(logged_in, contact):
    _, account = logged_in
    set_module(account, "orders", False)
    with pytest.raises(ActionError, match="orders"):
        run_action("create_order", {"account": account}, account=account, contact=contact,
                   items=[{"name": "X", "unit_price": Decimal("1.00"), "quantity": 1}])


@pytest.mark.django_db
def test_actions_still_run_with_no_row_or_enabled(logged_in, contact):  # regression
    _, account = logged_in
    assert "lead_id" in run_action("create_lead", {"account": account}, account=account, contact=contact)
    set_module(account, "sales", True)
    assert "lead_id" in run_action("create_lead", {"account": account}, account=account, contact=contact)


@pytest.mark.django_db
def test_modules_gate_independently(logged_in, contact):
    _, account = logged_in
    set_module(account, "orders", False)          # commerce off must not block crm
    assert "lead_id" in run_action("create_lead", {"account": account}, account=account, contact=contact)


@pytest.mark.django_db
def test_context_without_account_is_unchanged(logged_in, contact):  # trusted internal call
    _, account = logged_in
    set_module(account, "sales", False)
    assert "lead_id" in run_action("create_lead", {}, account=account, contact=contact)


# ---- public workflow API --------------------------------------------------------
@pytest.mark.django_db
def test_workflow_api_blocked_when_automation_disabled(client, db):
    from apps.accounts.models import Account as A

    acc = A.objects.create(company_name="Api Co")
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"),
                               max_emails_per_month=100, email_apis=True, api_rate_per_min=0)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    _, key = EmailApiKey.create_for_account(acc, name="k")
    assert client.get("/api/v1/workflows", HTTP_X_API_KEY=key).status_code == 200
    set_module(acc, "automations", False)
    assert client.get("/api/v1/workflows", HTTP_X_API_KEY=key).status_code == 403

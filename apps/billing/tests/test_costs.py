"""Unit economics: only email and AI are Akilent's costs; a paid plan that can lose money needs an
explicit, audited "I accept"."""
from decimal import Decimal

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.accounts.models import Account
from apps.billing import api as billing_api
from apps.billing.models import Plan, PlanLimit, Subscription
from apps.core.models import AdminAction


@pytest.fixture
def plan(db):
    return Plan.objects.create(slug="pro-c", name="Pro C", price_monthly=Decimal("20"), max_emails_per_month=10000)


def set_limit(plan, key, value):
    PlanLimit.objects.update_or_create(plan=plan, key=key, defaults={"value": value})


@pytest.fixture
def costs(db):
    billing_api.save_cost_settings(unit_costs_by_driver={"email": Decimal("0.0001"), "ai_action": Decimal("0.002")},
                                   fixed=Decimal("3"), target_margin_pct=50)


@pytest.mark.django_db
def test_worst_case_matches_a_hand_calculation(plan, costs, settings):
    settings.AI_DAILY_CALL_LIMIT = 100
    set_limit(plan, "emails_day", 200)        # 200 x 30 = 6,000 < 10,000 a month
    set_limit(plan, "ai_actions_day", 50)     # 50 x 30 = 1,500 (under the site ceiling of 100/day)
    e = billing_api.plan_economics(plan)
    # 6,000 x 0.0001 = 0.60; 1,500 x 0.002 = 3.00; fixed 3.00 -> 6.60
    assert e["worst_cost"] == Decimal("6.60")
    assert e["margin"] == Decimal("13.40") and e["margin_pct"] == 67
    assert not e["can_lose_money"] and not e["below_target"]


@pytest.mark.django_db
def test_the_site_ai_ceiling_caps_an_unlimited_plan(plan, costs, settings):
    settings.AI_DAILY_CALL_LIMIT = 500
    set_limit(plan, "ai_actions_day", -1)
    ai = next(line for line in billing_api.plan_economics(plan)["lines"] if line["driver"].key == "ai_action")
    assert ai["units"] == 15000


@pytest.mark.django_db
def test_uncapped_costly_limit_is_a_loss_risk_but_whatsapp_never_is(plan, costs, settings):
    settings.AI_DAILY_CALL_LIMIT = 0
    set_limit(plan, "ai_actions_day", 50)
    set_limit(plan, "whatsapp_marketing_msgs", -1)   # Meta bills the business: not Akilent's cost
    set_limit(plan, "conversations_month", -1)
    assert billing_api.loss_reasons(plan) == []
    set_limit(plan, "emails_month", -1)
    set_limit(plan, "emails_day", -1)
    assert any("Email" in r for r in billing_api.loss_reasons(plan))


@pytest.mark.django_db
def test_free_or_hidden_plans_are_never_blocked(plan, costs, settings):
    settings.AI_DAILY_CALL_LIMIT = 0
    set_limit(plan, "emails_month", -1)
    assert billing_api.loss_reasons(plan, price=Decimal("0")) == []
    assert billing_api.loss_reasons(plan, is_active=False) == []


@pytest.fixture
def op_client(client, db):
    client.force_login(User.objects.create_superuser("root", "r@x.com", "pw"))
    return client


@pytest.mark.django_db
def test_limits_that_lose_money_need_an_audited_accept(op_client, plan, costs, settings):
    settings.AI_DAILY_CALL_LIMIT = 0
    set_limit(plan, "emails_day", -1)
    form = {f"l:{plan.pk}:emails_month": "-1", "apply": "1"}
    op_client.post("/manage/plans/limits/", form)
    assert PlanLimit.objects.get(plan=plan, key="emails_month").value == 10000, "refused without the tick"
    op_client.post("/manage/plans/limits/", dict(form, accept_loss="on"))
    assert PlanLimit.objects.get(plan=plan, key="emails_month").value == -1
    assert AdminAction.objects.filter(action="plan.loss_acknowledged", target=plan.slug).exists()


@pytest.mark.django_db
def test_showing_a_loss_making_plan_is_refused(op_client, plan, costs, settings):
    settings.AI_DAILY_CALL_LIMIT = 0
    set_limit(plan, "emails_month", -1)
    set_limit(plan, "emails_day", -1)
    Plan.objects.filter(pk=plan.pk).update(is_active=False)
    op_client.post(f"/manage/plans/{plan.pk}/toggle/")
    plan.refresh_from_db()
    assert plan.is_active is False


@pytest.mark.django_db
def test_actual_cost_is_usage_times_unit_cost(plan, costs):
    acc = Account.objects.create(company_name="Cost Co", slug="cost-co")
    Subscription.objects.update_or_create(account=acc, defaults={
        "plan": plan, "status": Subscription.ACTIVE, "current_period_start": timezone.now()})
    for i in range(1000):
        billing_api.reserve(acc, "emails_month", operation_id=f"e{i}")
    for i in range(10):
        billing_api.reserve(acc, "ai_actions_day", operation_id=f"a{i}")
    cost = billing_api.actual_cost(acc)
    # 1,000 x 0.0001 = 0.10; 10 x 0.002 = 0.02; fixed 3.00
    assert cost["total"] == Decimal("3.12") and cost["margin"] == Decimal("16.88")


@pytest.mark.django_db
def test_costs_page_renders_and_saves(op_client, plan, costs):
    page = op_client.get("/manage/plans/costs/").content.decode()
    assert "Worst case per plan" in page and "Pro C" in page and "Meta" in page
    op_client.post("/manage/plans/costs/", {"cost:email": "0.0002", "cost:ai_action": "0.001",
                                            "fixed": "2", "target": "40"})
    assert billing_api.unit_costs()["email"] == Decimal("0.0002")
    assert AdminAction.objects.filter(action="costs.save").exists()

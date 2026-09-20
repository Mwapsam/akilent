"""Phase 0: the legacy Plan fallback must not reference Plan fields that don't exist."""
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.billing import api as billing_api
from apps.billing.models import ModuleSubscription, Plan, Subscription


def test_every_legacy_plan_map_attribute_is_a_real_plan_field():
    plan_fields = {f.name for f in Plan._meta.get_fields()}
    missing = {feature: attr for feature, attr in billing_api.LEGACY_PLAN_MAP.items()
               if attr not in plan_fields}
    assert not missing, f"LEGACY_PLAN_MAP points at Plan fields that do not exist: {missing}"


@pytest.fixture
def account(db):
    acc = Account.objects.create(company_name="Acme")
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"), email_apis=True)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    return acc


@pytest.mark.django_db
def test_legacy_fallback_still_answers_for_real_plan_fields(account):  # regression
    assert billing_api.has_feature(account, "email") is True          # Plan.email_apis
    assert billing_api.has_feature(account, "bulk_email") is False    # Plan.bulk_email default


@pytest.mark.django_db
def test_module_subscription_takes_precedence_over_legacy_fallback(account):  # regression
    assert billing_api.has_feature(account, "whatsapp") is False      # no module row, no plan flag
    ModuleSubscription.objects.create(account=account, module="whatsapp", enabled=True)
    assert billing_api.has_feature(account, "whatsapp") is True

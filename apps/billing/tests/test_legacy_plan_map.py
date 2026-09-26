"""The old Plan capability columns each map to a real catalog feature, and the LimitChecker shim
answers from the catalog (PlanFeature), not from the columns."""
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.billing import features as catalog
from apps.billing.limits import LimitChecker
from apps.billing.models import Plan, PlanFeature, Subscription


def test_every_legacy_flag_is_a_real_plan_field_and_a_catalog_feature():
    plan_fields = {f.name for f in Plan._meta.get_fields()}
    assert set(catalog.LEGACY_FLAGS) <= plan_fields
    assert set(catalog.LEGACY_FLAGS.values()) <= set(catalog.BY_KEY)


@pytest.fixture
def account(db):
    acc = Account.objects.create(company_name="Acme")
    plan = Plan.objects.create(slug="p", name="P", price_monthly=Decimal("10"), email_apis=True)
    Subscription.objects.create(account=acc, plan=plan, status=Subscription.ACTIVE,
                                current_period_start=timezone.now())
    return acc


@pytest.mark.django_db
def test_a_new_plan_starts_from_its_columns(account):
    checker = LimitChecker(account)
    assert checker.has_feature("email_apis") is True       # column on -> email_sending
    assert checker.has_feature("bulk_email") is False      # column off -> no email_campaigns


@pytest.mark.django_db
def test_the_shim_follows_the_matrix_not_the_columns(account):
    plan = account.subscription.plan
    PlanFeature.objects.create(plan=plan, key="email_campaigns")
    assert LimitChecker(Account.objects.get(pk=account.pk)).has_feature("bulk_email") is True
    PlanFeature.objects.filter(plan=plan, key="email_sending").delete()
    assert LimitChecker(Account.objects.get(pk=account.pk)).has_feature("email_apis") is False


@pytest.mark.django_db
def test_the_shim_still_needs_an_active_subscription(account):
    Subscription.objects.filter(account=account).update(status=Subscription.EXPIRED)
    assert LimitChecker(Account.objects.get(pk=account.pk)).has_feature("email_apis") is False

from decimal import Decimal

import pytest
from django.utils import timezone

from apps.accounts.models import Account
from apps.billing.limits import LimitChecker, PlanLimitExceeded
from apps.billing.models import Plan, Subscription


@pytest.fixture
def account(db):
    return Account.objects.create(company_name="Limit Co")


def _active_sub(account, **plan_kwargs):
    defaults = dict(
        slug="p", name="P", price_monthly=Decimal("10"), max_emails_per_month=2
    )
    defaults.update(plan_kwargs)
    plan = Plan.objects.create(**defaults)  # its limits are seeded from these columns
    return Subscription.objects.create(
        account=account,
        plan=plan,
        status=Subscription.ACTIVE,
        current_period_start=timezone.now(),
    )


@pytest.mark.django_db
def test_no_subscription_blocks(account):
    with pytest.raises(PlanLimitExceeded):
        LimitChecker(account).check_email("email:a")


@pytest.mark.django_db
def test_email_quota_enforced(account):
    _active_sub(account, max_emails_per_month=2)
    LimitChecker(account).check_email("email:a")
    LimitChecker(account).check_email("email:b")
    with pytest.raises(PlanLimitExceeded, match="Monthly email limit of 2"):
        LimitChecker(account).check_email("email:c")


@pytest.mark.django_db
def test_the_same_email_is_never_counted_twice(account):
    _active_sub(account, max_emails_per_month=1)
    LimitChecker(account).check_email("email:a")
    LimitChecker(account).check_email(
        "email:a"
    )  # a retry of the same message: no raise


@pytest.mark.django_db
def test_a_released_email_gives_its_unit_back(account):
    _active_sub(account, max_emails_per_month=1)
    checker = LimitChecker(account)
    checker.check_email("email:a")
    checker.settle_email("email:a", ok=False)
    checker.check_email("email:b")  # room again


@pytest.mark.django_db
def test_unlimited_quota_never_blocks(account):
    _active_sub(account, max_emails_per_month=-1)
    for i in range(5):
        LimitChecker(account).check_email(f"email:{i}")


@pytest.mark.django_db
def test_feature_gate(account):
    _active_sub(account, tracking_webhooks=False)
    checker = LimitChecker(account)
    assert checker.has_feature("tracking_webhooks") is False
    with pytest.raises(PlanLimitExceeded):
        checker.require_feature("tracking_webhooks", "tracking")

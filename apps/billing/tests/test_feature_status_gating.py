import pytest

from apps.billing import api as billing_api
from apps.billing.models import PlanFeature, Subscription

KEY = "automations"


@pytest.fixture
def plan_with_feature(plan):
    PlanFeature.objects.get_or_create(plan=plan, key=KEY)
    return plan


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,expected",
    [
        (Subscription.ACTIVE, True),
        (Subscription.TRIALING, True),
        (Subscription.PAST_DUE, True),  # grace period
        (Subscription.INCOMPLETE, False),
        (Subscription.EXPIRED, False),
        (Subscription.CANCELLED, False),
    ],
)
def test_plan_features_follow_subscription_status(
    account, plan_with_feature, subscription, status, expected
):
    from apps.billing import features as catalog

    if catalog.get(KEY).access_mode == catalog.CORE:
        pytest.skip("automations is a core feature in this catalog")
    subscription.status = status
    subscription.save()
    assert billing_api.entitled(account, KEY) is expected


@pytest.mark.django_db
def test_operator_grant_survives_a_lapsed_plan(
    account, plan_with_feature, subscription
):
    from apps.billing import features as catalog

    feature = catalog.get(KEY)
    if not catalog.overridable(feature.key):
        pytest.skip("not an overridable feature")
    subscription.status = Subscription.EXPIRED
    subscription.save()
    billing_api.set_override(account, KEY, grant=True, note="pilot", by=None)
    assert billing_api.entitled(account, KEY)

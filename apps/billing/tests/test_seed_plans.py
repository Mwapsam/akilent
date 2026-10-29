"""seed_plans can run again without changing anything, and every seeded plan has Content Studio
and sells every channel."""

import pytest
from django.core.management import call_command

from apps.billing.models import Plan, PlanFeature, PlanLimit

pytestmark = pytest.mark.django_db


def _snapshot():
    return {
        p.slug: (
            p.price_monthly,
            sorted(PlanFeature.objects.filter(plan=p).values_list("key", flat=True)),
            dict(PlanLimit.objects.filter(plan=p).values_list("key", "value")),
        )
        for p in Plan.objects.all()
    }


def test_running_twice_changes_nothing():
    call_command("seed_plans")
    first = _snapshot()
    call_command("seed_plans")
    assert _snapshot() == first
    assert set(first) == {"trial", "starter", "professional", "business"}
    assert all("content_studio" in features for _, features, _ in first.values())
    channels = {"whatsapp", "instagram", "facebook", "email_sending"}
    assert all(channels <= set(features) for _, features, _ in first.values())


def test_running_again_keeps_the_channels_an_operator_unticked():
    from apps.billing import api as billing_api

    call_command("seed_plans")
    starter = Plan.objects.get(slug="starter")
    billing_api.set_plan_channels(starter, ["email"])
    call_command("seed_plans")
    assert billing_api.plan_channel_keys(starter) == {"email"}

"""seed_plans can run again without changing anything, and every seeded plan has Content Studio."""

import pytest
from django.core.management import call_command

from apps.billing.models import Plan, PlanFeature, PlanLimit

pytestmark = pytest.mark.django_db


def _snapshot():
    return {
        p.slug: (
            p.price_monthly,
            p.service_type,
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
    assert all("content_studio" in features for _, _, features, _ in first.values())

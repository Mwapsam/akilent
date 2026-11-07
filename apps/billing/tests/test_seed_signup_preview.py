"""seed_signup_preview copies a live signup page's plans and channels into a dev database,
and --undo puts back what it changed."""

import json
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.billing.models import Plan, PlanFeature, PlanLimit
from apps.core import channels as ch
from apps.core.models import SiteSettings

CONFIG = {
    "channelChoices": [{"value": "instagram"}, {"value": "facebook"}],
    "plans": [
        {
            "slug": "trial",
            "name": "Trial",
            "priceMonthly": 0,
            "trialDays": 14,
            "channels": ["facebook", "instagram"],
            "compare": [
                {"label": "100 conversations/mo", "ok": True},
                {"label": "Detailed insights", "ok": True},
            ],
        },
        {
            "slug": "starter",
            "name": "Starter",
            "priceMonthly": 11.9,
            "trialDays": 0,
            "channels": ["facebook"],
            "compare": [
                {"label": "1,000 conversations/mo", "ok": True},
                {"label": "Detailed insights", "ok": False},
            ],
        },
    ],
    "included": ["Automations"],
}


@pytest.fixture
def page(tmp_path, settings):
    settings.DEBUG = True
    settings.BASE_DIR = tmp_path
    path = tmp_path / "signup.html"
    path.write_text(
        '<script id="signup-wizard-config" type="application/json">'
        f"{json.dumps(CONFIG)}</script>",
        encoding="utf-8",
    )
    ch._cache.update(value=None, loaded_at=0.0, failed_at=0.0)
    yield path
    ch._cache.update(value=None, loaded_at=0.0, failed_at=0.0)


@pytest.mark.django_db
def test_seed_copies_plans_and_channels_then_undo_restores(page):
    starter = Plan.objects.create(slug="starter", name="Old", price_monthly=19)
    PlanFeature.objects.create(plan=starter, key="insights")
    site = SiteSettings.load()
    before = (list(site.disabled_channels), list(site.restricted_channels))

    call_command("seed_signup_preview", file=str(page))

    starter.refresh_from_db()
    assert (starter.name, starter.price_monthly) == ("Starter", Decimal("11.90"))
    keys = set(PlanFeature.objects.filter(plan=starter).values_list("key", flat=True))
    assert "insights" not in keys and "automations" in keys
    assert PlanLimit.objects.get(plan=starter, key="conversations_month").value == 1000
    trial = Plan.objects.get(slug="trial")
    assert trial.trial_days == 14
    assert PlanFeature.objects.filter(plan=trial, key="insights").exists()
    assert ch.public_channels() == {"instagram", "facebook"}

    call_command("seed_signup_preview", undo=True)

    starter.refresh_from_db()
    assert (starter.name, starter.price_monthly) == ("Old", Decimal("19.00"))
    assert PlanFeature.objects.filter(plan=starter, key="insights").exists()
    site.refresh_from_db()
    assert (site.disabled_channels, site.restricted_channels) == before
    assert not Plan.objects.get(slug="trial").is_active  # created by the seed


@pytest.mark.django_db
def test_seed_refuses_without_debug(page, settings):
    settings.DEBUG = False
    with pytest.raises(CommandError, match="Development only"):
        call_command("seed_signup_preview", file=str(page))

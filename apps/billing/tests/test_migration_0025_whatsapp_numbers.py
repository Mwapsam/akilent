"""0025 caps every plan at one WhatsApp number (no per-number routing yet)."""

import importlib
from decimal import Decimal

import pytest
from django.apps import apps

migration = importlib.import_module(
    "apps.billing.migrations.0025_cap_whatsapp_numbers_at_one"
)


@pytest.mark.django_db
def test_every_plan_is_capped_at_one_number_and_crowded_accounts_are_listed(capsys):
    from apps.accounts.models import Account
    from apps.billing.models import Plan, PlanLimit
    from apps.whatsapp.models.tenant import WhatsAppBusinessNumber

    growth = Plan.objects.create(
        slug="growth-x",
        name="Growth",
        price_monthly=Decimal("50"),
        max_whatsapp_numbers=10,
    )
    PlanLimit.objects.update_or_create(
        plan=growth, key="whatsapp_numbers", defaults={"value": 10}
    )
    account = Account.objects.create(company_name="Two Numbers")
    for pnid in ("111", "222"):
        WhatsAppBusinessNumber.objects.create(
            account=account, phone_number_id=pnid, is_active=True
        )

    migration.forwards(apps, None)

    assert set(
        PlanLimit.objects.filter(key="whatsapp_numbers").values_list("value", flat=True)
    ) == {1}
    growth.refresh_from_db()
    assert growth.max_whatsapp_numbers == 1
    assert (
        WhatsAppBusinessNumber.objects.filter(account=account, is_active=True).count()
        == 2
    )
    assert f"account {account.pk} has 2 active" in capsys.readouterr().out

"""
Data migration: ensure exactly one platform account exists.

Looks for an existing account flagged as platform account; if none, creates one.
Fails loudly if multiple accounts already have is_platform_account=True (that
would violate the DB constraint and indicates data corruption).
"""

from django.db import migrations


def seed_platform_account(apps, schema_editor):
    Account = apps.get_model("accounts", "Account")

    existing = list(Account.objects.filter(is_platform_account=True))
    if len(existing) > 1:
        raise RuntimeError(
            "Multiple platform accounts found — cannot determine canonical one. "
            "Fix this manually before running migrations."
        )
    if len(existing) == 1:
        # Already seeded; nothing to do.
        return

    Account.objects.create(
        company_name="Akilent Platform",
        slug="akilent-platform",
        is_platform_account=True,
        is_active=True,
    )


def unseed_platform_account(apps, schema_editor):
    Account = apps.get_model("accounts", "Account")
    Account.objects.filter(slug="akilent-platform", is_platform_account=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0012_account_is_platform_account"),
    ]

    operations = [
        migrations.RunPython(
            seed_platform_account, reverse_code=unseed_platform_account
        ),
    ]

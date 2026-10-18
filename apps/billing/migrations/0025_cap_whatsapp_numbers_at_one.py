"""Cap every plan at one WhatsApp number until per-number routing exists.

Sends, read receipts, rate limiting and template sync all use an account's first
number, and contacts and conversations don't record which business number the
customer wrote to. With a second number a business could reply from the wrong one
(Meta refuses it, error 131047). Decided 2026-10-18; raise the limit again once
routing is per number.

Accounts that already have more than one active number keep them (nothing is
disconnected); they are listed in the migration output for review. Operator
overrides (AccountLimitOverride) are left as they are.

The key is written out so this migration keeps meaning what it meant when it ran.
"""

from django.db import migrations

KEY = "whatsapp_numbers"


def forwards(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanLimit = apps.get_model("billing", "PlanLimit")
    WhatsAppBusinessNumber = apps.get_model("whatsapp", "WhatsAppBusinessNumber")

    for plan in Plan.objects.all():
        PlanLimit.objects.update_or_create(plan=plan, key=KEY, defaults={"value": 1})
    Plan.objects.update(max_whatsapp_numbers=1)

    from django.db.models import Count

    crowded = (
        WhatsAppBusinessNumber.objects.filter(is_active=True)
        .values("account_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    for row in crowded:
        print(
            f"\n  review: account {row['account_id']} has {row['n']} active WhatsApp "
            "numbers; replies go from its first ready number only"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0024_plans_include_instagram"),
        ("whatsapp", "0022_webhookeventlog_fingerprint_webhookeventlog_held_and_more"),
    ]
    # Not reversed: by then an operator may have set limits on purpose.
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

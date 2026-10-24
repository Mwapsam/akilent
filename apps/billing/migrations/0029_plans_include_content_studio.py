"""Give every existing plan the Content Studio feature. This is not a pricing decision.

Content Studio was open to every business before it had a catalog key. Gating it behind
``content_studio`` without giving plans the key would lock everyone out, so every plan keeps
it (the user decided on 2026-10-24 that it is included on all plans). New plans get it through
``MODULE_FEATURES`` (signals.py).

The key is written out rather than imported from the catalog, so this migration keeps meaning
what it meant when it ran.
"""

from django.db import migrations

KEY = "content_studio"


def forwards(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanFeature = apps.get_model("billing", "PlanFeature")
    for plan in Plan.objects.all():
        PlanFeature.objects.get_or_create(plan=plan, key=KEY)


class Migration(migrations.Migration):
    dependencies = [("billing", "0028_paypal_support")]
    # Not reversed: by then an operator may have ticked or unticked it on purpose.
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

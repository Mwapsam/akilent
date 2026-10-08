"""Give every existing plan the Instagram feature. This is not a pricing decision.

The ``instagram`` feature was added to the catalog to gate connecting an account, but no plan
was given it, so every business on a plan lost Instagram overnight. Before the gate everyone could
use it, so every plan keeps it; untick it in the feature matrix to change that.

The key is written out rather than imported from the catalog, so this migration keeps meaning
what it meant when it ran.
"""

from django.db import migrations

KEY = "instagram"


def forwards(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanFeature = apps.get_model("billing", "PlanFeature")
    for plan in Plan.objects.all():
        PlanFeature.objects.get_or_create(plan=plan, key=KEY)


class Migration(migrations.Migration):
    dependencies = [("billing", "0023_unit_costs")]
    # Not reversed: by then an operator may have ticked or unticked it on purpose.
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

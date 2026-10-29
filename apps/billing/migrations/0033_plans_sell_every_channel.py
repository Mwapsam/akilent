"""Give every existing plan all four channel features.

Plans used to sell WhatsApp and Email through ``service_type`` (dropped in 0032), and WhatsApp
was a core feature every plan had. Now each channel is a plan feature like any other, and every
channel is sold (decided 2026-10-28, before any paying customer), so every plan starts with all
of them. The operator unticks a channel in the feature matrix to stop selling it on a plan.

The keys are written out rather than imported from the catalog, so this migration keeps meaning
what it meant when it ran.
"""

from django.db import migrations

KEYS = ("whatsapp", "instagram", "facebook", "email_sending")


def forwards(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanFeature = apps.get_model("billing", "PlanFeature")
    for plan in Plan.objects.all():
        for key in KEYS:
            PlanFeature.objects.get_or_create(plan=plan, key=key)


class Migration(migrations.Migration):
    dependencies = [("billing", "0032_drop_service_type")]
    # Not reversed: by then an operator may have ticked or unticked a channel on purpose.
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

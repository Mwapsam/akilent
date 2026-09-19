from django.db import migrations, models


def backfill(apps, schema_editor):
    # verification_pin is only ever stored after a successful registration.
    Number = apps.get_model("whatsapp", "WhatsAppBusinessNumber")
    for n in Number.objects.exclude(verification_pin__isnull=True).exclude(verification_pin=""):
        n.registration_status = "registered"
        n.save(update_fields=["registration_status"])


class Migration(migrations.Migration):

    dependencies = [
        ("whatsapp", "0010_whatsappcontact_contact"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsappbusinessnumber",
            name="registration_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("registering", "Registering"),
                    ("registered", "Registered"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="whatsappbusinessnumber",
            name="registration_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

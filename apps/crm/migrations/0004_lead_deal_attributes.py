from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0003_lead_open_uniqueness"),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="attributes",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="deal",
            name="attributes",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

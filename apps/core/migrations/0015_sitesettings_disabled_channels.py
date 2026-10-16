from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_platform_audit_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="disabled_channels",
            field=models.JSONField(blank=True, default=list),
        ),
    ]

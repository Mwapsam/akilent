# Generated migration for processing_started_at field

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0008_add_reconciliation_log"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentpostvariant",
            name="processing_started_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the provider first entered PROCESSING state",
                null=True,
            ),
        ),
        migrations.AddIndex(
            model_name="contentpostvariant",
            index=models.Index(
                fields=["processing_started_at", "status"],
                name="publishing__processings_idx",
            ),
        ),
    ]

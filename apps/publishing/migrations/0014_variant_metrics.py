"""Per-channel performance numbers: a snapshot history and the latest read per channel.

Channels published in the last 28 days are queued for a first read straight away. The
old post-level Facebook reach/impressions came from metrics Meta has since removed, so
they are not carried over.
"""

from datetime import timedelta

import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def queue_recent_channels(apps, schema_editor):
    ContentPostVariant = apps.get_model("publishing", "ContentPostVariant")
    now = timezone.now()
    ContentPostVariant.objects.filter(
        status="published",
        platform__in=("facebook", "instagram"),
        published_at__gte=now - timedelta(days=28),
    ).exclude(publication_id="").update(metrics_next_at=now)


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0013_variant_dispatched_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentpostvariant",
            name="metrics_latest",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="contentpostvariant",
            name="metrics_collected_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contentpostvariant",
            name="metrics_next_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.CreateModel(
            name="VariantMetricSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("collected_at", models.DateTimeField(db_index=True)),
                ("values", models.JSONField(default=dict)),
                ("states", models.JSONField(default=dict)),
                (
                    "error_type",
                    models.CharField(blank=True, default="", max_length=50),
                ),
                ("error", models.TextField(blank=True, default="")),
                (
                    "variant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="metric_snapshots",
                        to="publishing.contentpostvariant",
                    ),
                ),
            ],
            options={"ordering": ["-collected_at"]},
        ),
        migrations.RunPython(queue_recent_channels, migrations.RunPython.noop),
    ]

from django.db import migrations
from django.utils import timezone


def reclaim_in_flight_containers(apps, schema_editor):
    """Legacy backfill copied a mid-processing reel's post status (PUBLISHING) onto its
    variant. The poller only selects PROCESSING, so move those rows back into it."""
    ContentPostVariant = apps.get_model("publishing", "ContentPostVariant")
    now = timezone.now()
    candidates = ContentPostVariant.objects.filter(
        platform="instagram",
        status="publishing",
        publication_id="",
        metadata__has_key="ig_container_id",
    )
    for variant in candidates:
        if (variant.metadata or {}).get("ig_container_id"):
            variant.status = "processing"
            variant.processing_started_at = now
            variant.save(update_fields=["status", "processing_started_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0009_contentpostvariant_processing_started_at"),
    ]

    operations = [
        migrations.RunPython(reclaim_in_flight_containers, migrations.RunPython.noop),
    ]

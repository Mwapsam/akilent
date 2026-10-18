"""Per-channel scheduling: variants carry their own SCHEDULED status.

Before this, a scheduled post's variants stayed DRAFT and the scheduler read the post's
time. Existing scheduled posts get their pending variants marked SCHEDULED at the post's
time, so they publish exactly as before.
"""

from django.db import migrations, models


def schedule_pending_variants(apps, schema_editor):
    ContentPost = apps.get_model("publishing", "ContentPost")
    ContentPostVariant = apps.get_model("publishing", "ContentPostVariant")
    for post in ContentPost.objects.filter(status="scheduled").iterator():
        ContentPostVariant.objects.filter(
            post=post, status__in=("draft", "scheduled")
        ).update(status="scheduled", scheduled_at=post.scheduled_at)


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0012_fb_connection_expired_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentpostvariant",
            name="dispatched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(schedule_pending_variants, migrations.RunPython.noop),
    ]

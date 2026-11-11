"""Bring old plan items in line with the rules Planning and the calendar now share.

An idea with a date is planned, a planned item without one is an idea, and an item
whose posts have all gone out is published (nothing marked it before).
"""

from django.db import migrations


def sync(apps, schema_editor):
    Item = apps.get_model("publishing", "ContentPlanItem")
    Item.objects.filter(plan_status="idea", planned_date__isnull=False).update(
        plan_status="planned"
    )
    Item.objects.filter(plan_status="planned", planned_date__isnull=True).update(
        plan_status="idea"
    )
    for item in (
        Item.objects.exclude(plan_status="published")
        .filter(posts__isnull=False)
        .distinct()
    ):
        statuses = set(item.posts.values_list("status", flat=True))
        if statuses == {"published"}:
            item.plan_status = "published"
            item.save(update_fields=["plan_status"])


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0028_repair_backfilled_channel_failures"),
    ]

    operations = [
        migrations.RunPython(sync, migrations.RunPython.noop),
    ]

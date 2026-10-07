from django.db import migrations, models
from django.db.models import Max


def backfill_last_inbound_at(apps, schema_editor):
    InstagramConversation = apps.get_model("instagram", "InstagramConversation")
    InstagramMessage = apps.get_model("instagram", "InstagramMessage")
    latest = (
        InstagramMessage.objects.filter(direction="inbound", conversation__isnull=False)
        .values("conversation_id")
        .annotate(last=Max("timestamp"))
    )
    for row in latest.iterator():
        InstagramConversation.objects.filter(pk=row["conversation_id"]).update(
            last_inbound_at=row["last"]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("instagram", "0005_alter_instagrammessage_message_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="instagramconversation",
            name="last_inbound_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_last_inbound_at, migrations.RunPython.noop),
    ]

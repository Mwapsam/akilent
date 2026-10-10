from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("instagram", "0012_default_moderation_rules"),
    ]

    operations = [
        migrations.AddField(
            model_name="outboundmessage",
            name="quick_replies",
            field=models.JSONField(
                default=list,
                blank=True,
                help_text=(
                    "Instagram quick-reply chips. Each item: "
                    '{"content_type": "text", "title": str, "payload": str}. '
                    "Max 13 chips, title ≤ 20 chars, payload ≤ 1000 chars."
                ),
            ),
        ),
    ]

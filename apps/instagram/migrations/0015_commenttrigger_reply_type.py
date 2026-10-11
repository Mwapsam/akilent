from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("instagram", "0014_alter_outboundmessage_quick_replies"),
    ]

    operations = [
        migrations.AddField(
            model_name="commenttrigger",
            name="reply_type",
            field=models.CharField(
                choices=[
                    ("dm", "Private DM (Comment to DM)"),
                    ("comment", "Public comment reply"),
                ],
                blank=True,
                default="dm",
                help_text="How to send the reply: private DM or a public comment reply.",
                max_length=10,
            ),
        ),
    ]

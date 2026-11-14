from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("chatbot", "0008_chatbot_seen_on"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatsession",
            name="handoff_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chatsession",
            name="handoff_reason",
            field=models.CharField(blank=True, max_length=20),
        ),
    ]

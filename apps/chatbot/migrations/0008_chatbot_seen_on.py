from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("chatbot", "0007_chatbot_ai_daily_limit"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatbotconfig",
            name="seen_on",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

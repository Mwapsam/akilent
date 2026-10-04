from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("chatbot", "0003_add_category"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatbotconfig",
            name="chatbot_type",
            field=models.CharField(
                choices=[("system", "System"), ("customer", "Customer")],
                default="customer",
                max_length=20,
            ),
        ),
    ]

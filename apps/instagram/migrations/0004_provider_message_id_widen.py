from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("instagram", "0003_phase3_triggers"),
    ]

    operations = [
        migrations.AlterField(
            model_name="outboundmessage",
            name="provider_message_id",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
    ]

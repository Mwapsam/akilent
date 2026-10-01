from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("whatsapp", "0018_whatsappcampaign_last_progress_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="whatsappbusinessnumber",
            name="registration_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="whatsappbusinessnumber",
            name="registration_locked_until",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]

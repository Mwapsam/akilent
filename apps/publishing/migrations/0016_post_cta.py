from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0015_post_attribution"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentpost",
            name="cta",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

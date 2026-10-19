from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("publishing", "0016_post_cta"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentpostvariant",
            name="options",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]

"""The company's social profiles, linked from the landing page footer when set."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0020_testimonial_logo_case_study")]
    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name=name,
            field=models.URLField(blank=True, default=""),
        )
        for name in ("linkedin_url", "x_url", "instagram_url", "facebook_url")
    ]

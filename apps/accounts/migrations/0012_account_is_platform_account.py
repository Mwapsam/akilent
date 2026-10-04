from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0011_pilotenrollment"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="is_platform_account",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="account",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_platform_account=True),
                fields=["is_platform_account"],
                name="only_one_platform_account",
            ),
        ),
    ]

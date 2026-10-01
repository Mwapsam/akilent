import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0010_business_context"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PilotEnrollment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("wave", models.PositiveSmallIntegerField(default=2)),
                ("enrolled_at", models.DateTimeField(auto_now_add=True)),
                (
                    "baseline_avg_response_seconds",
                    models.FloatField(blank=True, null=True),
                ),
                ("baseline_lead_count", models.IntegerField(default=0)),
                ("baseline_conversation_count", models.IntegerField(default=0)),
                ("notes", models.TextField(blank=True, default="")),
                (
                    "account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pilot_enrollment",
                        to="accounts.account",
                    ),
                ),
                (
                    "enrolled_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pilot_enrollments",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["enrolled_at"],
            },
        ),
    ]

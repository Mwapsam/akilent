import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("email_service", "0005_emaildomain_dmarc_ok_emaildomain_last_checked_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailTrackingToken",
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
                ("token", models.CharField(db_index=True, max_length=64, unique=True)),
                ("recipient", models.EmailField()),
                ("url", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tracking_tokens",
                        to="email_service.emailmessage",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["created_at"], name="email_servi_created_tracking_idx"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="EmailTrackingEvent",
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
                (
                    "kind",
                    models.CharField(
                        choices=[("open", "Open"), ("click", "Click")],
                        max_length=10,
                    ),
                ),
                ("url", models.TextField(blank=True, default="")),
                ("ip", models.GenericIPAddressField(blank=True, null=True)),
                ("ua", models.CharField(blank=True, default="", max_length=512)),
                ("occurred_at", models.DateTimeField(auto_now_add=True)),
                (
                    "message",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="tracking_events",
                        to="email_service.emailmessage",
                    ),
                ),
            ],
            options={
                "ordering": ["-occurred_at"],
                "indexes": [
                    models.Index(
                        fields=["message", "kind"], name="email_servi_message_kind_idx"
                    ),
                    models.Index(
                        fields=["occurred_at"], name="email_servi_occurred_at_idx"
                    ),
                ],
            },
        ),
    ]

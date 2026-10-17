import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Replace the QR link table with a registration challenge that is consumed atomically.

    Reversing recreates PasskeyLinkRequest exactly as migration 0019 defined it.
    """

    dependencies = [
        ("accounts", "0019_passkey_link_request"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PasskeyRegistrationChallenge",
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
                ("challenge", models.CharField(max_length=128)),
                ("rp_id", models.CharField(max_length=255)),
                ("origin", models.CharField(max_length=255)),
                ("session_digest", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="passkey_registrations",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.DeleteModel(
            name="PasskeyLinkRequest",
        ),
    ]

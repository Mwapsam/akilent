from __future__ import annotations

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.accounts.fields
import apps.publishing.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounts", "0013_seed_platform_account"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ContentPost",
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
                    "public_id",
                    models.CharField(
                        default=apps.publishing.models._post_id,
                        editable=False,
                        max_length=45,
                        unique=True,
                    ),
                ),
                (
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="content_posts",
                        to="accounts.account",
                    ),
                ),
                ("platforms", models.JSONField(default=list)),
                (
                    "media_type",
                    models.CharField(
                        choices=[
                            ("image", "Image"),
                            ("video", "Video"),
                            ("reel", "Reel"),
                            ("story", "Story"),
                            ("text", "Text (Facebook only)"),
                            ("carousel", "Carousel"),
                        ],
                        max_length=10,
                    ),
                ),
                ("caption", models.TextField(blank=True, default="")),
                ("hashtags", models.JSONField(default=list)),
                ("media_urls", models.JSONField(default=list)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("scheduled", "Scheduled"),
                            ("publishing", "Publishing"),
                            ("published", "Published"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=12,
                    ),
                ),
                (
                    "scheduled_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ("published_at", models.DateTimeField(blank=True, null=True)),
                (
                    "ig_media_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "ig_container_id",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("fb_post_id", models.CharField(blank=True, default="", max_length=64)),
                ("error", models.TextField(blank=True, default="")),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["-scheduled_at", "-created_at"]},
        ),
        migrations.CreateModel(
            name="ContentCaption",
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
                    "post",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="captions",
                        to="publishing.contentpost",
                    ),
                ),
                ("prompt", models.TextField()),
                ("body", models.TextField()),
                ("hashtags", models.JSONField(default=list)),
                ("accepted", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="FacebookPageConnection",
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
                    "public_id",
                    models.CharField(
                        default=apps.publishing.models._fb_conn_id,
                        editable=False,
                        max_length=40,
                        unique=True,
                    ),
                ),
                (
                    "account",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="facebook_page_connection",
                        to="accounts.account",
                    ),
                ),
                ("page_id", models.CharField(blank=True, default="", max_length=64)),
                ("page_name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "access_token",
                    apps.accounts.fields.EncryptedTextField(blank=True, null=True),
                ),
                ("token_expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("disconnected", "Disconnected"),
                        ],
                        default="active",
                        max_length=20,
                    ),
                ),
                (
                    "oauth_state",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("oauth_state_created_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]

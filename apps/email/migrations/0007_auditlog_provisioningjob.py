"""Add AuditLog and ProvisioningJob models.

These are append-only/tracking tables:
  - ProvisioningJob: lifecycle of async Celery provisioning operations
  - AuditLog: immutable record of every provider operation

Both have FK to accounts.Account and indexes tuned for the expected query
patterns (filter by account+status, resource_type+resource_id, timestamp).
"""

from __future__ import annotations

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("email_service", "0006_emailtrackingevent_emailtrackingtoken"),
        ("accounts", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProvisioningJob",
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
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="provisioning_jobs",
                        to="accounts.account",
                    ),
                ),
                (
                    "job_type",
                    models.CharField(
                        choices=[
                            ("provision_domain", "Provision Domain"),
                            ("deprovision_domain", "Deprovision Domain"),
                            ("provision_mailbox", "Provision Mailbox"),
                            ("deprovision_mailbox", "Deprovision Mailbox"),
                            ("change_password", "Change Password"),
                            ("set_quota", "Set Quota"),
                            ("rotate_dkim", "Rotate DKIM"),
                            ("suspend_mailbox", "Suspend Mailbox"),
                            ("provision_alias", "Provision Alias"),
                            ("deprovision_alias", "Deprovision Alias"),
                        ],
                        max_length=50,
                    ),
                ),
                ("resource_type", models.CharField(max_length=50)),
                ("resource_id", models.CharField(max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("running", "Running"),
                            ("success", "Success"),
                            ("failed", "Failed"),
                            ("retrying", "Retrying"),
                        ],
                        default="pending",
                        max_length=20,
                    ),
                ),
                (
                    "celery_task_id",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("error", models.TextField(blank=True, default="")),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("metadata", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="provisioningjob",
            index=models.Index(
                fields=["account", "status"],
                name="email_servi_account_pjob_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="provisioningjob",
            index=models.Index(
                fields=["resource_type", "resource_id"],
                name="email_servi_pjob_resource_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="provisioningjob",
            index=models.Index(
                fields=["created_at"],
                name="email_servi_pjob_created_at_idx",
            ),
        ),
        migrations.CreateModel(
            name="AuditLog",
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
                    "account",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="email_audit_logs",
                        to="accounts.account",
                    ),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("action", models.CharField(max_length=100)),
                ("resource_type", models.CharField(max_length=50)),
                ("resource_id", models.CharField(max_length=255)),
                ("success", models.BooleanField(default=True)),
                ("error", models.TextField(blank=True, default="")),
                ("metadata", models.JSONField(default=dict)),
                ("timestamp", models.DateTimeField(auto_now_add=True, db_index=True)),
            ],
            options={
                "ordering": ["-timestamp"],
            },
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(
                fields=["account", "timestamp"],
                name="email_servi_audit_account_ts_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(
                fields=["resource_type", "resource_id"],
                name="email_servi_audit_resource_idx",
            ),
        ),
    ]

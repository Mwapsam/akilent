"""Automation rules and configuration.

AutomationRule defines WHEN/IF/THEN automation workflows: when a trigger event
occurs with matching conditions, execute the specified action (send message,
create contact, etc.).
"""
from django.db import models


class AutomationRule(models.Model):
    """A rule that fires actions when trigger events match conditions.

    The rule engine is deterministic-first: all conditions are evaluated
    synchronously without AI. AI-augmented conditions (Phase 5) are optional
    and gate separately.
    """

    class TriggerEvent(models.TextChoices):
        MESSAGE_RECEIVED = "message_received", "Message received"
        MESSAGE_SENT = "message_sent", "Message sent"
        LEAD_CREATED = "lead_created", "Lead created"
        DEAL_STAGE_CHANGED = "deal_stage_changed", "Deal stage changed"

    account = models.ForeignKey("accounts.Account", on_delete=models.CASCADE)

    name = models.CharField(max_length=255)
    trigger_event = models.CharField(max_length=50, choices=TriggerEvent.choices)

    conditions = models.JSONField(default=dict)
    action = models.JSONField(default=dict)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "whatsapp_automationrule"  # Pinned to original table to avoid data migration
        indexes = [
            models.Index(fields=["account", "trigger_event", "is_active"]),
        ]

    def __str__(self):
        return f"{self.name} ({self.trigger_event})"


# ── Lifecycle workflows (Phase 6) ────────────────────────────────────────────
# A separate, richer model from AutomationRule (which stays pinned to the
# legacy WhatsApp table). ``definition`` holds a node graph — see
# apps.automation.workflow_engine for the shape and executor.

import secrets as _secrets

from django.utils.text import slugify as _slugify


def _workflow_run_public_id() -> str:
    return "wfr_" + _secrets.token_hex(16)


class Workflow(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="workflows"
    )
    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    version = models.PositiveIntegerField(default=1)
    definition = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "slug"], name="uniq_workflow_account_slug"
            )
        ]
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _slugify(self.name)[:160]
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} [{self.status} v{self.version}]"


class WorkflowRun(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        WAITING = "waiting", "Waiting"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    public_id = models.CharField(
        max_length=40, unique=True, default=_workflow_run_public_id, editable=False
    )
    workflow = models.ForeignKey(
        Workflow, on_delete=models.CASCADE, related_name="runs"
    )
    contact = models.ForeignKey(
        "contacts.Contact", on_delete=models.CASCADE, related_name="workflow_runs"
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    context = models.JSONField(default=dict, blank=True)
    current_step = models.CharField(max_length=64, blank=True, default="")
    next_due_at = models.DateTimeField(blank=True, null=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workflow", "contact"],
                condition=models.Q(status__in=["active", "waiting"]),
                name="uniq_active_run_per_contact",
            )
        ]
        indexes = [
            models.Index(fields=["status", "next_due_at"]),
            models.Index(fields=["workflow", "status"]),
        ]

    def __str__(self):
        return f"{self.workflow_id}:{self.contact_id} [{self.status}]"


class WorkflowStepRun(models.Model):
    run = models.ForeignKey(
        WorkflowRun, on_delete=models.CASCADE, related_name="step_runs"
    )
    step_id = models.CharField(max_length=64)
    step_type = models.CharField(max_length=32)
    status = models.CharField(max_length=12, default="ok")
    result = models.JSONField(default=dict, blank=True)
    executed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["run", "step_id"])]

    def __str__(self):
        return f"{self.run_id}/{self.step_id} ({self.step_type})"

from django.conf import settings
from django.db import models
from django.utils import timezone


class WhatsAppCampaign(models.Model):
    """One bulk WhatsApp template send to a Contact List — the WhatsApp side of
    the channel-neutral "Campaigns" concept (R1.5c). Deliberately MVP-sized
    next to ``apps.email.models.BulkEmailCampaign``: no CSV upload, no
    per-recipient variable overrides, no pause/resume, no plan-limit checks.
    Those get added only if a pilot needs them — see docs/plans R1.5c.

    Sending reuses ``apps.automation.workflows.send_whatsapp_message`` (the
    same function Workflows and the legacy AutomationRule action call), so
    rate limiting, the 24h-window/consent policy, and delivery-status
    tracking all come for free from the existing OutboundMessage queue —
    this model only tracks the campaign as a unit.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="whatsapp_campaigns"
    )
    name = models.CharField(max_length=150)
    contact_list = models.ForeignKey(
        "contacts.ContactList", on_delete=models.PROTECT, related_name="whatsapp_campaigns"
    )
    template = models.ForeignKey(
        "whatsapp.MessageTemplate", on_delete=models.PROTECT, related_name="campaigns"
    )
    # {"<template variable>": "contact.<attribute>" | literal string} — same
    # convention as a Workflow send_whatsapp step's variable_mapping, minus the
    # "context.<key>" form (a campaign has no workflow run context).
    variable_mapping = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    recipient_count = models.PositiveIntegerField(default=0)
    queued_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["account", "status"])]

    def mark_sending(self) -> None:
        self.status = self.Status.SENDING
        self.started_at = timezone.now()
        self.save(update_fields=["status", "started_at"])

    def mark_completed(self, *, queued: int, skipped: int) -> None:
        self.status = self.Status.COMPLETED
        self.queued_count = queued
        self.skipped_count = skipped
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "queued_count", "skipped_count", "completed_at"])

    def mark_failed(self, error: str) -> None:
        self.status = self.Status.FAILED
        self.error = error[:5000]
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "error", "completed_at"])

    def __str__(self):
        return f"WhatsApp campaign {self.name!r} ({self.status})"

from django.conf import settings
from django.db import models
from django.utils import timezone

from .outbound import OutboundMessage


class WhatsAppCampaign(models.Model):
    """One bulk WhatsApp template send to a Contact List — the WhatsApp side of
    the channel-neutral "Campaigns" concept (R1.5c).

    Fanned out via ``WhatsAppCampaignRecipient`` (one row per contact, created
    up front) and a bounded, self-rescheduling chunk task
    (``apps.whatsapp.campaigns.send_campaign``), mirroring
    ``apps.email.models.BulkEmailCampaign``/``BulkEmailRecipient`` — restart-safe
    for lists of thousands of contacts, unlike a single inline loop.

    ``queued_count``/``skipped_count`` here are the *processing* summary (has
    Akilent gotten to this recipient yet, and what did it decide) — never
    delivery status. Delivery (sent/delivered/read/failed at Meta) is read
    live from ``OutboundMessage``/``MessageLog`` via
    ``apps.whatsapp.campaigns.campaign_delivery_stats``, a deliberately
    separate query so the two concepts never blend into one number.
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
        "contacts.ContactList",
        on_delete=models.PROTECT,
        related_name="whatsapp_campaigns",
    )
    template = models.ForeignKey(
        "whatsapp.MessageTemplate", on_delete=models.PROTECT, related_name="campaigns"
    )
    # {"<template variable>": "contact.<attribute>" | "account.<attr>" |
    # "business.<fact>" | literal string} — resolved by
    # apps.automation.variables.resolve, the same resolver the workflow
    # editor's send_whatsapp step and engagement starters use.
    variable_mapping = models.JSONField(default=dict, blank=True)
    # {"<template variable>": "<text to use when this contact has no value>"}.
    variable_fallbacks = models.JSONField(default=dict, blank=True)

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
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
    # Touched every time send_campaign claims/dispatches a chunk for this
    # campaign — the signal apps.whatsapp.tasks.sweep_stuck_campaigns uses to
    # tell "still working, just a big list" from "the chunk chain died"
    # (e.g. an on_commit-scheduled .delay() that never reached the broker).
    last_progress_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["account", "status"])]

    def mark_sending(self) -> None:
        """Idempotent: called at the start of every chunk, but only the first
        call actually transitions status/sets started_at."""
        if self.status in (self.Status.DRAFT, self.Status.QUEUED):
            self.status = self.Status.SENDING
            if self.started_at is None:
                self.started_at = timezone.now()
            self.save(update_fields=["status", "started_at"])

    def touch_progress(self) -> None:
        self.last_progress_at = timezone.now()
        self.save(update_fields=["last_progress_at"])

    def mark_completed(self) -> None:
        self.status = self.Status.COMPLETED
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "completed_at"])

    def mark_failed(self, error: str) -> None:
        self.status = self.Status.FAILED
        self.error = error[:5000]
        self.completed_at = timezone.now()
        self.save(update_fields=["status", "error", "completed_at"])

    def increment_counts(self, *, queued: int = 0, skipped: int = 0) -> None:
        """Processing summary only — never delivery status. Uses F() expressions
        so concurrent chunk writes (across different campaigns) can't race."""
        updates = []
        if queued:
            self.queued_count = models.F("queued_count") + queued
            updates.append("queued_count")
        if skipped:
            self.skipped_count = models.F("skipped_count") + skipped
            updates.append("skipped_count")
        if updates:
            self.save(update_fields=updates)
            self.refresh_from_db(fields=updates)

    def __str__(self):
        return f"WhatsApp campaign {self.name!r} ({self.status})"


class WhatsAppCampaignRecipient(models.Model):
    """One contact's fan-out row for a WhatsAppCampaign — processing state only.

    Distinct from delivery: ``status`` here answers "did Akilent act on this
    recipient, and how" (never sent to Meta / created a message / deliberately
    skipped). What Meta reported back about a QUEUED recipient's message lives
    on ``message.status``/``message.message_log.status``, never here — see
    ``apps.whatsapp.campaigns.campaign_delivery_stats``.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUEUED = "queued", "Queued"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"
        HELD = "held", "Held"

    class SkipReason(models.TextChoices):
        OPTED_OUT = "opted_out", "Opted out"
        NO_WHATSAPP_IDENTITY = "no_whatsapp_identity", "No WhatsApp number"
        MISSING_VALUE = "missing_value", "Missing template value"

    campaign = models.ForeignKey(
        WhatsAppCampaign, on_delete=models.CASCADE, related_name="recipients"
    )
    contact = models.ForeignKey("contacts.Contact", on_delete=models.CASCADE)
    message = models.OneToOneField(
        OutboundMessage,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="campaign_recipient",
    )
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    skip_reason = models.CharField(
        max_length=25, choices=SkipReason.choices, blank=True, default=""
    )
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "contact"],
                name="unique_whatsapp_campaign_contact",
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "status"]),
            models.Index(fields=["campaign", "skip_reason"]),
        ]

    def __str__(self):
        return f"{self.campaign_id}:{self.contact_id} [{self.status}]"

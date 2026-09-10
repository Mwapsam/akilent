"""ScheduledJob — the one scheduling primitive behind "send later", scheduled
campaigns, and scheduled WhatsApp.

A ScheduledJob is a thin orchestration row: a timer (``fire_at`` UTC), a
validated payload (``template_payload`` — the create-kwargs to replay), and a
pointer to whatever it materialises. It re-implements no validation: at fire
time the drainer calls the existing chokepoints
(apps.api.services.create_and_queue_message / create_and_queue_campaign), so
quota, reputation, suppression, MX and verified-domain checks all run exactly as
for an immediate send.
"""
from __future__ import annotations

import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


def _job_public_id() -> str:
    return "job_" + secrets.token_hex(16)


class ScheduledJob(models.Model):
    class Kind(models.TextChoices):
        EMAIL_SINGLE = "email_single", "Email (single)"
        EMAIL_CAMPAIGN = "email_campaign", "Email campaign"
        WHATSAPP = "whatsapp", "WhatsApp"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SCHEDULED = "scheduled", "Scheduled"
        PROCESSING = "processing", "Processing"
        RUNNING = "running", "Running"
        SENT = "sent", "Sent"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed"

    #: Non-terminal statuses the drainer / edit paths may act on.
    ACTIVE_STATUSES = {Status.SCHEDULED, Status.PROCESSING, Status.RUNNING}
    TERMINAL_STATUSES = {Status.SENT, Status.COMPLETED, Status.CANCELLED, Status.FAILED}

    MAX_ATTEMPTS = 5
    PROCESSING_STALE = timedelta(minutes=10)

    public_id = models.CharField(
        max_length=40, unique=True, default=_job_public_id, editable=False
    )
    account = models.ForeignKey(
        "accounts.Account", on_delete=models.CASCADE, related_name="scheduled_jobs"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.SCHEDULED, db_index=True
    )

    # UTC instant of the next fire. ``tz`` keeps the original IANA zone (or the
    # "recipient" sentinel) for display, reschedule, and DST-safe recurrence.
    fire_at = models.DateTimeField(db_index=True)
    tz = models.CharField(max_length=64, default="UTC")

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, blank=True, null=True
    )

    # Validated create-kwargs, replayed at fire time (and re-resolved per
    # occurrence for recurring jobs).
    template_payload = models.JSONField(default=dict, blank=True)
    # uuid4 hex, generated at schedule time, threaded into create_and_queue_*
    # so a materialise-retry after a mid-fire crash cannot double-create.
    idempotency_key = models.CharField(max_length=64, unique=True)

    target_message = models.ForeignKey(
        "email_service.EmailMessage", on_delete=models.SET_NULL,
        blank=True, null=True, related_name="+",
    )
    target_campaign = models.ForeignKey(
        "email_service.BulkEmailCampaign", on_delete=models.SET_NULL,
        blank=True, null=True, related_name="scheduled_jobs",
    )
    target_outbound = models.ForeignKey(
        "whatsapp.OutboundMessage", on_delete=models.SET_NULL,
        blank=True, null=True, related_name="+",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, blank=True, null=True, related_name="children"
    )

    # Recurrence (RFC-5545). Empty => one-shot.
    recurrence = models.CharField(max_length=255, blank=True, default="")
    recurrence_until = models.DateTimeField(blank=True, null=True)
    max_occurrences = models.PositiveIntegerField(blank=True, null=True)
    occurrence_count = models.PositiveIntegerField(default=0)

    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(blank=True, null=True)
    stale_grace_secs = models.PositiveIntegerField(default=21600)  # 6h

    fired_at = models.DateTimeField(blank=True, null=True)
    error = models.TextField(blank=True, default="")
    result = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["fire_at", "id"]
        indexes = [
            models.Index(fields=["status", "fire_at"]),
            models.Index(fields=["account", "status", "fire_at"]),
        ]

    def __str__(self):
        return f"{self.public_id} {self.kind} [{self.status}] @ {self.fire_at:%Y-%m-%d %H:%M}"

    # --- state transitions ---------------------------------------------------

    def mark_processing(self) -> None:
        self.status = self.Status.PROCESSING
        self.save(update_fields=["status", "updated_at"])

    def mark_sent(self, result: dict | None = None, *, running: bool = False) -> None:
        self.status = self.Status.RUNNING if running else self.Status.SENT
        self.fired_at = timezone.now()
        self.error = ""
        if result:
            self.result = {**(self.result or {}), **result}
        self.save(update_fields=["status", "fired_at", "error", "result", "updated_at"])

    def mark_failed(self, error: str, *, terminal: bool = False) -> None:
        """Record a fire failure.

        ``terminal=True`` (unverified domain, suppression, deleted template)
        goes straight to FAILED. Otherwise the job is re-armed with exponential
        backoff until MAX_ATTEMPTS.
        """
        self.attempts += 1
        self.error = (error or "")[:5000]
        if terminal or self.attempts >= self.MAX_ATTEMPTS:
            self.status = self.Status.FAILED
            self.next_attempt_at = None
        else:
            self.status = self.Status.SCHEDULED
            delay = min(2 * (2 ** self.attempts), 3600)
            self.next_attempt_at = timezone.now() + timedelta(seconds=delay)
        self.save(update_fields=[
            "attempts", "error", "status", "next_attempt_at", "updated_at",
        ])

    def reschedule(self, fire_at, tz: str) -> None:
        self.fire_at = fire_at
        self.tz = tz
        self.attempts = 0
        self.next_attempt_at = None
        self.error = ""
        self.status = self.Status.SCHEDULED
        self.save(update_fields=[
            "fire_at", "tz", "attempts", "next_attempt_at", "error", "status",
            "updated_at",
        ])

    def cancel(self) -> None:
        self.status = self.Status.CANCELLED
        self.save(update_fields=["status", "updated_at"])

    def arm_next_occurrence(self) -> bool:
        """Re-arm a recurring job for its next occurrence.

        Returns True if re-armed, False if the recurrence is exhausted (caller
        then marks the job COMPLETED).
        """
        if not self.recurrence:
            return False

        from apps.core.scheduling import next_occurrence

        self.occurrence_count += 1
        if self.max_occurrences and self.occurrence_count >= self.max_occurrences:
            return False

        nxt = next_occurrence(self.recurrence, after=self.fire_at, tz=self.tz)
        if nxt is None:
            return False
        if self.recurrence_until and nxt > self.recurrence_until:
            return False

        self.fire_at = nxt
        self.status = self.Status.SCHEDULED
        self.attempts = 0
        self.next_attempt_at = None
        self.error = ""
        self.save(update_fields=[
            "fire_at", "status", "attempts", "next_attempt_at", "error",
            "occurrence_count", "updated_at",
        ])
        return True

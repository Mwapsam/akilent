from django.db import models
from django.utils import timezone


class FacebookWebhookEventLog(models.Model):
    """
    Raw Facebook Page webhook event stored before async processing.

    Idempotency is enforced by the unique constraint on (account, event_id); the
    event_id is derived from what Meta identifies the item by (a message's mid, a
    comment's id and verb).
    """

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"
        HELD = "held", "Held (channel switched off)"

    class EventType(models.TextChoices):
        MESSAGE = "message", "Messenger message"
        COMMENT = "comment", "Comment"
        UNKNOWN = "unknown", "Unknown"

    account = models.ForeignKey(
        "accounts.Account",
        on_delete=models.CASCADE,
        related_name="facebook_webhook_events",
        null=True,
        blank=True,
    )
    page_id = models.CharField(max_length=64, blank=True, default="")
    event_id = models.CharField(max_length=255, db_index=True)
    event_type = models.CharField(
        max_length=20, choices=EventType.choices, default=EventType.UNKNOWN
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.RECEIVED
    )
    raw_payload = models.JSONField()
    attempts = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")

    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "event_id"],
                condition=models.Q(account__isnull=False),
                name="unique_facebook_webhook_event",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "received_at"]),
            models.Index(fields=["account", "event_type"]),
        ]

    def mark_processing(self):
        self.status = self.Status.PROCESSING
        self.save(update_fields=["status"])

    def mark_processed(self):
        self.status = self.Status.PROCESSED
        self.processed_at = timezone.now()
        self.save(update_fields=["status", "processed_at"])

    def mark_failed(self, error: str):
        self.attempts += 1
        self.status = self.Status.FAILED
        self.error_message = error[:5000]
        self.save(update_fields=["attempts", "status", "error_message"])

    def __str__(self):
        return f"Facebook:{self.event_type} ({self.status})"

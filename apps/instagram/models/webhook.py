from django.db import models
from django.utils import timezone


class WebhookEventLog(models.Model):
    """
    Raw Instagram webhook event stored before async processing.

    Idempotency is enforced at the DB level via the unique constraint on
    (instagram_account, event_id). The event_id is a deterministic key derived
    from the payload: {account_id}:{platform_object_id}:{event_type}.
    """

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSING = "processing", "Processing"
        PROCESSED = "processed", "Processed"
        FAILED = "failed", "Failed"

    class EventType(models.TextChoices):
        MESSAGE = "message", "DM Message"
        COMMENT = "comment", "Comment"
        MENTION = "mention", "Mention"
        STATUS = "status", "Status Update"
        UNKNOWN = "unknown", "Unknown"

    instagram_account = models.ForeignKey(
        "instagram.InstagramBusinessAccount",
        on_delete=models.CASCADE,
        related_name="webhook_events",
        null=True,
        blank=True,
    )
    # Deterministic idempotency key — prevents duplicate processing
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
                fields=["instagram_account", "event_id"],
                condition=models.Q(instagram_account__isnull=False),
                name="unique_instagram_webhook_event",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "received_at"]),
            models.Index(fields=["instagram_account", "event_type"]),
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
        return f"Instagram:{self.event_type} ({self.status})"

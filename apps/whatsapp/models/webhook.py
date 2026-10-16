from django.db import models
from django.utils import timezone


class WebhookEventLog(models.Model):
    class Source(models.TextChoices):
        WHATSAPP = "whatsapp", "WhatsApp"

    source = models.CharField(max_length=50, choices=Source.choices)
    event_type = models.CharField(max_length=100)

    payload = models.JSONField()

    processed = models.BooleanField(default=False)
    attempts = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)

    # True while the channel is switched off: stored and acknowledged, processed on replay.
    held = models.BooleanField(default=False, db_index=True)
    # sha256 of the raw body. Meta redelivers identical bodies, so a repeat is dropped.
    fingerprint = models.CharField(max_length=64, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["processed", "created_at"]),  # retry worker
            models.Index(fields=["source", "event_type"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "fingerprint"],
                condition=~models.Q(fingerprint=""),
                name="uniq_whatsapp_webhook_fingerprint",
            )
        ]

    def mark_processed(self):
        self.processed = True
        self.processed_at = timezone.now()
        self.save(update_fields=["processed", "processed_at"])

    def mark_failed(self, error: str):
        self.attempts += 1
        self.error_message = error[:5000]
        self.save(update_fields=["attempts", "error_message"])

    def __str__(self):
        return (
            f"{self.source}:{self.event_type} ({'ok' if self.processed else 'pending'})"
        )

from django.db import models


class ConnectionTest(models.Model):
    """A "verify connection" send for one number.

    Evidence for setup progress: a successful test means outbound works, and an
    inbound message from the same recipient afterwards means the webhook works.
    Tied to the number explicitly (MessageLog is per-account) so an unrelated
    customer message can't complete onboarding.
    """

    class Status(models.TextChoices):
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    number = models.ForeignKey(
        "whatsapp.WhatsAppBusinessNumber",
        on_delete=models.CASCADE,
        related_name="connection_tests",
    )
    recipient = models.CharField(max_length=20)  # E.164
    status = models.CharField(max_length=10, choices=Status.choices)
    message_id = models.CharField(max_length=255, blank=True, default="")
    error_code = models.CharField(max_length=32, blank=True, default="")
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["number", "status", "-created_at"])]
